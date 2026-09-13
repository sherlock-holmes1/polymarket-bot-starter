"""BTC/USD price feed.

Primary source: Polymarket Real-Time Data Socket (RTDS) — Chainlink topic.
This is the SAME price stream Polymarket's BTC Up/Down 15M markets resolve
against (the proposer reads the Chainlink BTC/USD value at the resolution
timestamp). Using it eliminates basis risk between your signal and the market.

Fallback source: Coinbase Advanced Trade WebSocket ticker channel. Free,
push-based, no auth, tracks Chainlink closely. Used automatically if RTDS
fails to deliver a price within `STARTUP_TIMEOUT_S`.

Both feeds maintain a shared in-memory price history (price, timestamp) tuples
keyed by source so consumers can compute deltas or switch sources at runtime.

Endpoint references:
- RTDS:     wss://ws-live-data.polymarket.com   (topic `crypto_prices_chainlink`)
- Coinbase: wss://advanced-trade-ws.coinbase.com (channel `ticker`)

See `docs/price-feeds/polymarket-rtds-reference.md` and
`docs/price-feeds/coinbase-advanced-trade-ws.md` for full WS protocol details.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from typing import Deque

from websocket import WebSocketApp

from src.utils import get_logger

logger = get_logger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"
COINBASE_URL = "wss://advanced-trade-ws.coinbase.com"

# How long after start_price_feed() before we declare RTDS dead and fail over
STARTUP_TIMEOUT_S = 15
# Retain ~30 minutes of price ticks; enough for any 15M-cycle lookback
_HISTORY_MAX_AGE_S = 1800

_lock = threading.Lock()
_history: Deque[tuple[float, float]] = deque()
_latest_price: float | None = None
_active_source: str = "(none)"

_rtds_thread: threading.Thread | None = None
_coinbase_thread: threading.Thread | None = None
_rtds_alive = threading.Event()


def get_latest_price() -> float | None:
    with _lock:
        return _latest_price


def get_active_source() -> str:
    with _lock:
        return _active_source


def get_price_delta_pct(lookback_seconds: int) -> float | None:
    """% change from `lookback_seconds` ago to now. None if insufficient history."""
    now = time.time()
    cutoff = now - lookback_seconds
    with _lock:
        if not _history:
            return None
        old = [(p, ts) for p, ts in _history if ts <= cutoff]
        if not old:
            return None
        oldest = old[-1][0]
        current = _history[-1][0]
    return ((current - oldest) / oldest) * 100


def _push_tick(price: float, source: str) -> None:
    global _latest_price, _active_source
    now = time.time()
    cutoff = now - _HISTORY_MAX_AGE_S
    with _lock:
        _latest_price = price
        _active_source = source
        _history.append((price, now))
        while _history and _history[0][1] < cutoff:
            _history.popleft()


# ----------------------------- RTDS (Chainlink) -----------------------------

def _rtds_on_open(ws: WebSocketApp) -> None:
    sub = {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": json.dumps({"symbol": "btc/usd"}),
            }
        ],
    }
    chainlink_key = os.getenv("CHAINLINK_RTDS_API_KEY")
    if chainlink_key:
        sub["subscriptions"][0]["chainlink_api_key"] = chainlink_key
    ws.send(json.dumps(sub))
    logger.info("RTDS subscribed: crypto_prices_chainlink btc/usd")

    # Start the 5-second PING keep-alive loop on a background thread.
    def _ping_loop() -> None:
        while ws.keep_running:
            try:
                ws.send("PING")
            except Exception:
                return
            time.sleep(5)

    threading.Thread(target=_ping_loop, daemon=True).start()


def _rtds_on_message(_ws: WebSocketApp, raw: str) -> None:
    if raw == "PONG":
        return
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    if msg.get("topic") != "crypto_prices_chainlink":
        return
    payload = msg.get("payload") or {}
    if (payload.get("symbol") or "").lower() != "btc/usd":
        return
    value = payload.get("value")
    if value is None:
        return
    _push_tick(float(value), "rtds-chainlink")
    if not _rtds_alive.is_set():
        _rtds_alive.set()
        logger.info(f"RTDS Chainlink first tick: ${float(value):,.2f}")


def _rtds_on_error(_ws: WebSocketApp, err: Exception) -> None:
    logger.warning(f"RTDS error: {err!r}")


def _rtds_loop() -> None:
    while True:
        ws = WebSocketApp(
            RTDS_URL,
            on_open=_rtds_on_open,
            on_message=_rtds_on_message,
            on_error=_rtds_on_error,
        )
        ws.run_forever(ping_interval=0)  # we drive PING manually per RTDS protocol
        logger.warning("RTDS disconnected; reconnecting in 2s")
        time.sleep(2)


# ----------------------------- Coinbase (fallback) -----------------------------

def _coinbase_on_open(ws: WebSocketApp) -> None:
    ws.send(json.dumps({
        "type": "subscribe",
        "product_ids": ["BTC-USD"],
        "channel": "ticker",
    }))
    logger.info("Coinbase WS subscribed: ticker BTC-USD")


def _coinbase_on_message(_ws: WebSocketApp, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    if msg.get("channel") != "ticker":
        return
    for event in msg.get("events", []):
        for t in event.get("tickers", []):
            if t.get("product_id") == "BTC-USD":
                price = float(t["price"])
                _push_tick(price, "coinbase")


def _coinbase_on_error(_ws: WebSocketApp, err: Exception) -> None:
    logger.warning(f"Coinbase WS error: {err!r}")


def _coinbase_loop() -> None:
    while True:
        ws = WebSocketApp(
            COINBASE_URL,
            on_open=_coinbase_on_open,
            on_message=_coinbase_on_message,
            on_error=_coinbase_on_error,
        )
        ws.run_forever(ping_interval=20, ping_timeout=10)
        logger.warning("Coinbase WS disconnected; reconnecting in 2s")
        time.sleep(2)


# ----------------------------- Public API -----------------------------

def start_price_feed(*, force_fallback: bool = False) -> None:
    """Start the RTDS Chainlink feed in the background and fail over to Coinbase
    if RTDS doesn't deliver a tick within STARTUP_TIMEOUT_S.

    `force_fallback=True` skips RTDS and uses Coinbase directly. Useful for
    environments that can't reach Polymarket's data plane.
    """
    global _rtds_thread, _coinbase_thread

    if force_fallback:
        _coinbase_thread = threading.Thread(target=_coinbase_loop, daemon=True)
        _coinbase_thread.start()
        logger.info("Price feed: Coinbase WS (forced)")
        return

    _rtds_thread = threading.Thread(target=_rtds_loop, daemon=True)
    _rtds_thread.start()

    def _failover_watch() -> None:
        if _rtds_alive.wait(timeout=STARTUP_TIMEOUT_S):
            return
        logger.warning(
            f"RTDS did not deliver a Chainlink BTC tick within {STARTUP_TIMEOUT_S}s — "
            "falling over to Coinbase. RTDS will keep retrying in the background "
            "and will become the active source again once it recovers."
        )
        global _coinbase_thread
        _coinbase_thread = threading.Thread(target=_coinbase_loop, daemon=True)
        _coinbase_thread.start()

    threading.Thread(target=_failover_watch, daemon=True).start()
    logger.info("Price feed: RTDS Chainlink (primary), Coinbase ready for failover")
