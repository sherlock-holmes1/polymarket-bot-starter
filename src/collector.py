"""Read-only recorder for one explicitly selected Polymarket market.

Milestone step 1-3: choose a market on purpose, record its full public trading
configuration with observation timestamps, then record raw book and trade events
with both venue and local clocks — reconnecting and flagging a data gap whenever
the stream drops, whether or not the market rolled.

No credentials, no order submission, no signing. Read-only collection stays
permitted where trading is not.

    python -m src.collector --list-rewarded
    python -m src.collector --slug btc-updown-15m-1789326900 --duration 900
    python -m src.collector --condition-id 0x0fa2… --output recordings
    python -m src.collector --btc-updown-15m          # follows the rolling window
"""
from __future__ import annotations

import argparse
import signal
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from py_clob_client_v2 import ClobClient

from src.market_channel import MarketChannelSupervisor
from src.market_spec import (
    MarketSpec,
    current_btc_updown_15m_slug,
    list_reward_eligible_markets,
    select_active_btc_updown_15m,
    select_market,
)
from src.recording import JsonlRecorder, utc_now_iso
from src.utils import get_logger

CLOB_BASE_URL = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137
DEFAULT_SPEC_REFRESH_S = 300

logger = get_logger(__name__)


class PublicMarketCollector:
    """Record public CLOB events and mark any period without a valid book."""

    def __init__(
        self,
        output_root: Path,
        resolve_market: Callable[[], MarketSpec],
        *,
        mode: str,
        poll_interval_s: int = 10,
        spec_refresh_s: int = DEFAULT_SPEC_REFRESH_S,
        market_rolled: Callable[[MarketSpec], bool] | None = None,
    ) -> None:
        self.output_root = output_root
        self._resolve_market = resolve_market
        self._mode = mode
        self._spec_refresh_s = spec_refresh_s
        self._market_rolled = market_rolled or (lambda _spec: False)
        self._lock = threading.Lock()
        self._spec: MarketSpec | None = None
        self._spec_observed_monotonic: float = 0.0
        self._recorder: JsonlRecorder | None = None
        self._subscribed_assets: set[str] = set()
        self._pending_snapshot_assets: set[str] = set()
        self._gap_open = False
        self._stopped = False
        self._supervisor = MarketChannelSupervisor(
            self._active_token_ids,
            self._on_market_event,
            poll_interval_s=poll_interval_s,
            on_connection_state=self._on_connection_state,
        )

    @property
    def recording_directory(self) -> Path | None:
        return None if self._recorder is None else self._recorder.directory

    def start(self) -> None:
        self._active_token_ids()
        if self._recorder is None or self._spec is None:
            raise RuntimeError("recorder was not initialized")
        self._recorder.record("collector_started", {
            "started_at": utc_now_iso(),
            "mode": self._mode,
            "condition_id": self._spec.condition_id,
            "market_slug": self._spec.market_slug,
        })
        self._supervisor.start()
        logger.info(f"Recording public market data in {self._recorder.directory}")

    def stop(self) -> None:
        self._stopped = True
        self._supervisor.stop()
        if self._recorder is not None:
            if self._gap_open:
                self._recorder.record("collector_gap", {
                    "state": "closed", "recovered_by": "collector_stopped",
                })
            self._recorder.record("collector_stopped", {"stopped_at": utc_now_iso()})
            self._recorder.close()

    def _active_token_ids(self) -> list[str]:
        """Resolve the market to subscribe to. Called by the channel supervisor."""
        spec = self._current_spec()
        with self._lock:
            self._subscribed_assets = set(spec.token_ids)
            self._pending_snapshot_assets = set(spec.token_ids)
        return spec.token_ids

    def _current_spec(self) -> MarketSpec:
        """Return the market spec, re-observing it when it is due or has rolled."""
        with self._lock:
            spec, observed = self._spec, self._spec_observed_monotonic
        if spec is not None:
            fresh_enough = time.monotonic() - observed < self._spec_refresh_s
            if fresh_enough and not self._market_rolled(spec):
                return spec
        fresh = self._resolve_market()
        with self._lock:
            previous, self._spec = self._spec, fresh
            self._spec_observed_monotonic = time.monotonic()
        if previous is None:
            self._open_recording(fresh)
        elif self._recorder is not None:
            self._recorder.record("market_spec", asdict(fresh))
            if previous.condition_id != fresh.condition_id:
                logger.info(f"Market rolled: {previous.market_slug} → {fresh.market_slug}")
        return fresh

    def _open_recording(self, spec: MarketSpec) -> None:
        directory = self.output_root / _recording_name(spec)
        market = asdict(spec)
        market.pop("raw", None)
        self._recorder = JsonlRecorder(directory, {
            "collector": "public_market_collector",
            "mode": self._mode,
            "market": market,
            "data_sources": [
                "CLOB market WebSocket wss://ws-subscriptions-clob.polymarket.com/ws/market",
                "CLOB GET /markets/{condition_id}",
            ],
            "clocks": {
                "exchange_timestamp_ms": "venue timestamp, present on most market events",
                "received_unix_ms": "local wall clock at receipt",
                "received_monotonic_ns": "local monotonic clock at receipt",
            },
        })
        self._recorder.record("market_spec", asdict(spec))
        logger.info(f"Selected market: {spec.describe()}")

    def _on_market_event(self, payload: dict[str, Any]) -> None:
        if self._recorder is None:
            raise RuntimeError("received market event before recorder initialization")
        self._recorder.record("market_event", payload)
        if payload.get("event_type") != "book":
            return
        with self._lock:
            self._pending_snapshot_assets.discard(str(payload.get("asset_id", "")))
            recovered = self._gap_open and not self._pending_snapshot_assets
            if recovered:
                self._gap_open = False
        if recovered:
            self._recorder.record("collector_gap", {
                "state": "closed", "recovered_by": "book_snapshot",
            })

    def _on_connection_state(self, state: str, details: dict[str, Any]) -> None:
        if self._stopped or self._recorder is None:
            return
        self._recorder.record("connection", {"state": state, **details})
        if state == "subscribed":
            with self._lock:
                self._subscribed_assets = set(details["token_ids"])
                self._pending_snapshot_assets = set(details["token_ids"])
            return
        if state != "disconnected":
            return
        with self._lock:
            newly_open = not self._gap_open
            self._gap_open = True
            # Every book is suspect after a drop: the gap stays open until a fresh
            # snapshot has arrived for *each* subscribed token, not just the first.
            self._pending_snapshot_assets = set(self._subscribed_assets)
        if newly_open:
            self._recorder.record("collector_gap", {
                "state": "opened", "reason": "connection_closed", **details,
            })


def _recording_name(spec: MarketSpec) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{now}-{spec.market_slug or spec.condition_id[:12]}"


def _build_resolver(
    client: ClobClient, args: argparse.Namespace
) -> tuple[Callable[[], MarketSpec], str, Callable[[MarketSpec], bool]]:
    """Return (resolver, mode, rolled-predicate) for the chosen selection mode.

    The rolled-predicate is a local computation — it decides whether the selected
    market is still the right one without spending a request to find out.
    """
    if args.btc_updown_15m:
        return (
            lambda: select_active_btc_updown_15m(client),
            "btc-updown-15m",
            lambda spec: current_btc_updown_15m_slug() != spec.market_slug,
        )
    never_rolls: Callable[[MarketSpec], bool] = lambda _spec: False
    if args.condition_id:
        return (lambda: select_market(client, condition_id=args.condition_id)), "fixed", never_rolls
    return (lambda: select_market(client, slug=args.slug)), "fixed", never_rolls


def _print_rewarded(client: ClobClient, limit: int) -> None:
    specs = list_reward_eligible_markets(client, min_daily_rate=0.0)
    print(f"{len(specs)} reward-enabled markets accepting orders (top {limit} by daily rate)\n")
    for spec in specs[:limit]:
        print(f"  {spec.rewards.daily_rate_total:>8.2f}/day  {spec.market_slug}")
        print(
            f"            condition_id={spec.condition_id} tick={spec.minimum_tick_size} "
            f"min_order_size={spec.minimum_order_size} "
            f"reward_min_size={spec.rewards.min_size} reward_max_spread={spec.rewards.max_spread}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Record public Polymarket market data (read-only).")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--condition-id", help="Record this market by condition ID")
    selector.add_argument("--slug", help="Record this market by slug")
    selector.add_argument("--btc-updown-15m", action="store_true",
                          help="Follow the rolling BTC Up/Down 15M window")
    selector.add_argument("--list-rewarded", action="store_true",
                          help="List reward-enabled markets and exit without recording")
    parser.add_argument("--output", type=Path, default=Path("recordings"))
    parser.add_argument("--poll-interval", type=int, default=10,
                        help="Seconds between market/connection checks (default: %(default)s)")
    parser.add_argument("--spec-refresh", type=int, default=DEFAULT_SPEC_REFRESH_S,
                        help="Seconds between market-config re-observations (default: %(default)s)")
    parser.add_argument("--duration", type=int, default=0,
                        help="Stop after this many seconds (0 = run until interrupted)")
    parser.add_argument("--limit", type=int, default=25, help="Rows for --list-rewarded")
    args = parser.parse_args()

    client = ClobClient(CLOB_BASE_URL, chain_id=POLYGON_CHAIN_ID)

    if args.list_rewarded:
        _print_rewarded(client, args.limit)
        return 0
    if not (args.condition_id or args.slug or args.btc_updown_15m):
        parser.error("select a market: --condition-id, --slug, --btc-updown-15m, or --list-rewarded")

    resolver, mode, rolled = _build_resolver(client, args)
    if args.btc_updown_15m:
        logger.info(f"Rolling mode — current window slug is {current_btc_updown_15m_slug()}")

    collector = PublicMarketCollector(
        args.output,
        resolver,
        mode=mode,
        poll_interval_s=args.poll_interval,
        spec_refresh_s=args.spec_refresh,
        market_rolled=rolled,
    )
    stopping = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    collector.start()
    deadline = time.monotonic() + args.duration if args.duration else None
    try:
        while not stopping.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                logger.info(f"Reached --duration {args.duration}s — stopping")
                break
            stopping.wait(1)
    finally:
        collector.stop()
    directory = collector.recording_directory
    if directory is not None:
        print(f"\nRecording written to {directory}")
        print(f"Replay it with:  python -m src.replay {directory} --verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
