"""Deterministic replay of recorded Polymarket level-two book events.

Replay rebuilds one local book per outcome token and decides, at every instant,
whether that book is fit to quote against. A book is quotable only when all of
these hold:

  * a snapshot has been applied since the last gap or inconsistency;
  * the recorder is not inside an open data gap;
  * both sides have at least one level;
  * the book is not crossed (best bid >= best ask);
  * the last update is no older than `max_stale_ms`.

Time spent in each rejection state is accounted for, so a recording that looks
busy but was unquotable for most of its length cannot pass as good data.

Fills are never inferred. A trade is counted only when the venue emitted a
`last_trade_price` event; a recorded price touching a quote proves nothing about
queue position and is not treated as an execution.
"""
from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from src.recording import EVENTS_FILENAME, METADATA_FILENAME, unix_ms_to_iso

# How long a book may go without an update and still be quoted against. This is a
# data-freshness cap, not a market-activity signal: without a per-asset heartbeat
# the feed cannot distinguish "nobody is trading" from "our stream died", so the
# default sits at three WebSocket ping intervals. Tune it with --max-stale-ms.
DEFAULT_MAX_STALE_MS = 30_000

BOOK_EVENT_TYPES = ("book", "price_change", "last_trade_price", "tick_size_change", "best_bid_ask")

# `new_market` and `market_resolved` are venue-wide broadcasts — they arrive for
# markets this recording never subscribed to, so they are counted apart from the
# subscribed assets' events.
BROADCAST_EVENT_TYPES = ("new_market", "market_resolved")

# A `book` snapshot's `timestamp` is the last change that affected the book, not
# the moment it was emitted, so it would read as tens of seconds of false latency.
LATENCY_EVENT_TYPES = ("price_change", "last_trade_price", "best_bid_ask", "tick_size_change")

REASON_QUOTABLE = "quotable"
REASON_NO_SNAPSHOT = "no_snapshot"
REASON_GAP_OPEN = "gap_open"
REASON_CROSSED = "crossed"
REASON_EMPTY_SIDE = "empty_side"
REASON_STALE = "stale"
REASON_DIVERGED = "diverged"
REJECT_REASONS = (
    REASON_NO_SNAPSHOT, REASON_GAP_OPEN, REASON_CROSSED, REASON_EMPTY_SIDE,
    REASON_STALE, REASON_DIVERGED,
)

# Every price_change carries the venue's own top of book *for that update*, so a
# locally rebuilt book can be checked against it on every delta — this is what
# catches a silently dropped update, which a determinism check cannot.
# One disagreement is a race, so a book is only rejected after this many in a row.
# Measured on a 70s BTC 15M recording: 4 disagreements in 22,290 checks (0.02%).
#
# `best_bid_ask` is deliberately NOT part of this counter. It is sampled at its
# own instant rather than in-band with an update, and disagreed with the rebuilt
# book on 18 of 58 events (31%) in the same recording. Mixing that noise into a
# drift counter would false-invalidate healthy books; it is reported instead.
DEFAULT_DIVERGENCE_TOLERANCE = 3

# The venue reports an empty side as bid 0 / ask 1.
EMPTY_BID, EMPTY_ASK = 0.0, 1.0


@dataclass
class OrderBook:
    """One outcome token's local book, rebuilt from snapshots and deltas."""

    bids: dict[str, float] = field(default_factory=dict)
    asks: dict[str, float] = field(default_factory=dict)
    has_snapshot: bool = False
    invalidated_by: str | None = None
    last_update_ms: int | None = None
    last_snapshot_sequence: int | None = None
    consecutive_divergences: int = 0

    def apply_snapshot(self, payload: dict[str, Any], *, sequence: int, at_ms: int | None) -> None:
        self.bids = _levels(payload.get("bids", []))
        self.asks = _levels(payload.get("asks", []))
        self.has_snapshot = True
        self.invalidated_by = None
        self.consecutive_divergences = 0
        self.last_snapshot_sequence = sequence
        self.last_update_ms = at_ms

    def apply_price_change(self, change: dict[str, Any], *, at_ms: int | None) -> None:
        side = change.get("side")
        levels = self.bids if side == "BUY" else self.asks if side == "SELL" else None
        if levels is None:
            raise ValueError(f"unknown price-change side: {side!r}")
        price, size = str(change["price"]), float(change["size"])
        if size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self.last_update_ms = at_ms

    def invalidate(self, reason: str) -> None:
        """Mark the book unfit to quote until a fresh snapshot arrives."""
        self.invalidated_by = reason

    def agrees_with_venue_top(self, venue_bid: Any, venue_ask: Any) -> bool | None:
        """Compare the local top of book with a venue-supplied one.

        None when the venue did not supply both sides — nothing to compare.
        """
        expected_bid, expected_ask = _optional_price(venue_bid), _optional_price(venue_ask)
        if expected_bid is None or expected_ask is None:
            return None
        mine_bid = EMPTY_BID if self.best_bid is None else self.best_bid
        mine_ask = EMPTY_ASK if self.best_ask is None else self.best_ask
        return abs(mine_bid - expected_bid) < 1e-9 and abs(mine_ask - expected_ask) < 1e-9

    def check_against_venue_top(
        self, venue_bid: Any, venue_ask: Any, *, tolerance: int
    ) -> bool:
        """Check the local book against the top supplied in-band with an update.

        A run of `tolerance` disagreements means the local book has drifted — a
        dropped delta, not a sampling race — so it stops being quotable until the
        next snapshot rebuilds it.
        """
        agreed = self.agrees_with_venue_top(venue_bid, venue_ask)
        if agreed is None:
            return True
        if agreed:
            self.consecutive_divergences = 0
            return True
        self.consecutive_divergences += 1
        if self.consecutive_divergences >= tolerance:
            self.invalidate(REASON_DIVERGED)
        return False

    @property
    def best_bid(self) -> float | None:
        return max((float(price) for price in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((float(price) for price in self.asks), default=None)

    @property
    def spread(self) -> float | None:
        bid, ask = self.best_bid, self.best_ask
        return None if bid is None or ask is None else round(ask - bid, 10)

    def depth_at_best(self) -> tuple[float, float]:
        best_bid = max(self.bids.items(), key=lambda level: float(level[0]), default=None)
        best_ask = min(self.asks.items(), key=lambda level: float(level[0]), default=None)
        return (best_bid[1] if best_bid else 0.0, best_ask[1] if best_ask else 0.0)

    def total_depth(self) -> tuple[float, float]:
        return sum(self.bids.values()), sum(self.asks.values())

    def reject_reason(self, now_ms: int | None, max_stale_ms: int) -> str:
        """Why this book cannot be quoted against, or REASON_QUOTABLE."""
        if self.invalidated_by is not None:
            return self.invalidated_by
        if not self.has_snapshot:
            return REASON_NO_SNAPSHOT
        bid, ask = self.best_bid, self.best_ask
        if bid is None or ask is None:
            return REASON_EMPTY_SIDE
        if bid >= ask:
            return REASON_CROSSED
        if now_ms is not None and self.last_update_ms is not None:
            if now_ms - self.last_update_ms > max_stale_ms:
                return REASON_STALE
        return REASON_QUOTABLE

    def stale_at_ms(self, max_stale_ms: int) -> int | None:
        return None if self.last_update_ms is None else self.last_update_ms + max_stale_ms


@dataclass
class AssetStats:
    label: str = ""
    snapshots: int = 0
    price_changes: int = 0
    trades: int = 0
    tick_size_changes: int = 0
    orphan_price_changes: int = 0
    crossed_observations: int = 0
    top_of_book_mismatches: int = 0
    best_bid_ask_disagreements: int = 0
    best_bid_ask_checks: int = 0
    state_ms: dict[str, int] = field(default_factory=dict)
    spreads: list[float] = field(default_factory=list)
    best_bid_depths: list[float] = field(default_factory=list)
    best_ask_depths: list[float] = field(default_factory=list)
    total_bid_depths: list[float] = field(default_factory=list)
    total_ask_depths: list[float] = field(default_factory=list)


@dataclass
class Gap:
    opened_at_ms: int | None
    closed_at_ms: int | None
    reason: str
    recovered_by: str | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.opened_at_ms is None or self.closed_at_ms is None:
            return None
        return self.closed_at_ms - self.opened_at_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "opened_at": unix_ms_to_iso(self.opened_at_ms) if self.opened_at_ms else None,
            "closed_at": unix_ms_to_iso(self.closed_at_ms) if self.closed_at_ms else None,
            "duration_s": None if self.duration_ms is None else round(self.duration_ms / 1000, 3),
            "reason": self.reason,
            "recovered_by": self.recovered_by,
            "open_at_end_of_recording": self.closed_at_ms is None,
        }


@dataclass
class ReplayReport:
    """Everything replay can assert about a recording. No fills are inferred."""

    events: int = 0
    started_at_ms: int | None = None
    ended_at_ms: int | None = None
    by_kind: dict[str, int] = field(default_factory=dict)
    by_event_type: dict[str, int] = field(default_factory=dict)
    assets: dict[str, AssetStats] = field(default_factory=dict)
    gaps: list[Gap] = field(default_factory=list)
    connection_states: dict[str, int] = field(default_factory=dict)
    exchange_latency_ms: list[int] = field(default_factory=list)
    broadcast_events: int = 0
    resolutions: list[dict[str, Any]] = field(default_factory=list)
    events_without_exchange_timestamp: int = 0
    out_of_order_exchange_timestamps: int = 0
    max_stale_ms: int = DEFAULT_MAX_STALE_MS
    digest: str = ""
    warnings: list[str] = field(default_factory=list)
    fills_inferred: bool = False

    @property
    def duration_ms(self) -> int:
        if self.started_at_ms is None or self.ended_at_ms is None:
            return 0
        return self.ended_at_ms - self.started_at_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_s": round(self.duration_ms / 1000, 3),
            "started_at": unix_ms_to_iso(self.started_at_ms) if self.started_at_ms else None,
            "ended_at": unix_ms_to_iso(self.ended_at_ms) if self.ended_at_ms else None,
            "events": self.events,
            "by_kind": dict(sorted(self.by_kind.items())),
            "by_event_type": dict(sorted(self.by_event_type.items())),
            "connection_states": dict(sorted(self.connection_states.items())),
            "gaps": {
                "count": len(self.gaps),
                "total_s": round(
                    sum(gap.duration_ms or 0 for gap in self.gaps) / 1000, 3
                ),
                "detail": [gap.to_dict() for gap in self.gaps],
            },
            "broadcast_events": self.broadcast_events,
            "resolutions": self.resolutions,
            "timestamps": {
                "events_without_exchange_timestamp": self.events_without_exchange_timestamp,
                "out_of_order_exchange_timestamps": self.out_of_order_exchange_timestamps,
                "exchange_to_receipt_ms": _summarize(
                    [float(value) for value in self.exchange_latency_ms]
                ),
            },
            "quoting": {
                "max_stale_ms": self.max_stale_ms,
                "assets": {
                    asset_id: _asset_to_dict(stats) for asset_id, stats in sorted(self.assets.items())
                },
            },
            "digest": self.digest,
            "fills_inferred": self.fills_inferred,
            "warnings": self.warnings,
        }


def replay(
    rows: Iterable[dict[str, Any]],
    *,
    max_stale_ms: int = DEFAULT_MAX_STALE_MS,
    divergence_tolerance: int = DEFAULT_DIVERGENCE_TOLERANCE,
    labels: dict[str, str] | None = None,
) -> ReplayReport:
    """Rebuild every book from `rows` and report what the recording supports."""
    report = ReplayReport(max_stale_ms=max_stale_ms)
    books: dict[str, OrderBook] = {}
    labels = labels or {}
    digest = hashlib.sha256()
    clock = _StateClock(books, report, max_stale_ms)
    gap_open: Gap | None = None
    last_exchange_ms: int | None = None

    for index, row in enumerate(rows, start=1):
        report.events += 1
        kind = str(row.get("kind", ""))
        payload = row.get("payload") or {}
        sequence = int(row.get("sequence") or index)
        now_ms = _row_time_ms(row)
        exchange_ms = row.get("exchange_timestamp_ms")

        if report.started_at_ms is None:
            report.started_at_ms = now_ms
        if now_ms is not None:
            report.ended_at_ms = now_ms
        report.by_kind[kind] = report.by_kind.get(kind, 0) + 1

        if exchange_ms is not None:
            exchange_ms = int(exchange_ms)

        clock.advance(now_ms)

        if kind == "collector_gap":
            state = str(payload.get("state", ""))
            if state == "opened":
                gap_open = Gap(
                    opened_at_ms=now_ms,
                    closed_at_ms=None,
                    reason=str(payload.get("reason") or payload.get("message") or "disconnected"),
                )
                report.gaps.append(gap_open)
                for book in books.values():
                    book.invalidate(REASON_GAP_OPEN)
            elif state == "closed" and gap_open is not None:
                gap_open.closed_at_ms = now_ms
                gap_open.recovered_by = str(payload.get("recovered_by") or "book_snapshot")
                gap_open = None
            continue

        if kind == "connection":
            state = str(payload.get("state", ""))
            report.connection_states[state] = report.connection_states.get(state, 0) + 1
            continue

        if kind != "market_event":
            continue

        event_type = str(payload.get("event_type") or "")
        report.by_event_type[event_type] = report.by_event_type.get(event_type, 0) + 1

        if event_type in LATENCY_EVENT_TYPES:
            if exchange_ms is None:
                report.events_without_exchange_timestamp += 1
            else:
                received = row.get("received_unix_ms")
                if received is not None:
                    report.exchange_latency_ms.append(int(received) - exchange_ms)
                if last_exchange_ms is not None and exchange_ms < last_exchange_ms:
                    report.out_of_order_exchange_timestamps += 1
                last_exchange_ms = exchange_ms if last_exchange_ms is None else max(exchange_ms, last_exchange_ms)

        if event_type in BROADCAST_EVENT_TYPES:
            report.broadcast_events += 1
            if event_type == "market_resolved":
                report.resolutions.append({
                    "market": payload.get("market"),
                    "slug": payload.get("slug"),
                    "winning_outcome": payload.get("winning_outcome"),
                    "winning_asset_id": payload.get("winning_asset_id"),
                    "at": unix_ms_to_iso(now_ms) if now_ms else None,
                })
            continue

        if event_type not in BOOK_EVENT_TYPES:
            continue

        # Book freshness is measured on the local receipt clock: our knowledge of
        # the book is only as current as the moment the message reached us.
        book_ms = now_ms
        touched: list[str] = []
        venue_top: dict[str, tuple[Any, Any]] = {}

        if event_type == "book":
            asset_id = str(payload["asset_id"])
            book = _book_for(books, report, asset_id, labels)
            book.apply_snapshot(payload, sequence=sequence, at_ms=book_ms)
            _stats(report, asset_id).snapshots += 1
            touched.append(asset_id)
        elif event_type == "price_change":
            for change in payload.get("price_changes") or []:
                asset_id = str(change["asset_id"])
                book = _book_for(books, report, asset_id, labels)
                stats = _stats(report, asset_id)
                if not book.has_snapshot:
                    stats.orphan_price_changes += 1
                    book.invalidate(REASON_NO_SNAPSHOT)
                    continue
                book.apply_price_change(change, at_ms=book_ms)
                stats.price_changes += 1
                touched.append(asset_id)
                venue_top[asset_id] = (change.get("best_bid"), change.get("best_ask"))
        elif event_type == "last_trade_price":
            asset_id = str(payload.get("asset_id") or "")
            _book_for(books, report, asset_id, labels)
            _stats(report, asset_id).trades += 1
        elif event_type == "tick_size_change":
            asset_id = str(payload.get("asset_id") or "")
            _book_for(books, report, asset_id, labels)
            _stats(report, asset_id).tick_size_changes += 1
        elif event_type == "best_bid_ask":
            # An independent read of the venue's top, sampled at its own instant.
            # Recorded as an observation — it never applies to the book and never
            # feeds the drift counter, because it disagrees far too often to be a
            # verdict on book health.
            asset_id = str(payload.get("asset_id") or "")
            book = _book_for(books, report, asset_id, labels)
            if book.has_snapshot:
                agreed = book.agrees_with_venue_top(
                    payload.get("best_bid"), payload.get("best_ask")
                )
                if agreed is not None:
                    stats = _stats(report, asset_id)
                    stats.best_bid_ask_checks += 1
                    if not agreed:
                        stats.best_bid_ask_disagreements += 1

        for asset_id in dict.fromkeys(touched):
            book = books[asset_id]
            if asset_id in venue_top:
                venue_bid, venue_ask = venue_top[asset_id]
                if not book.check_against_venue_top(
                    venue_bid, venue_ask, tolerance=divergence_tolerance
                ):
                    _stats(report, asset_id).top_of_book_mismatches += 1
            reason = book.reject_reason(book_ms, max_stale_ms)
            if reason == REASON_CROSSED:
                _stats(report, asset_id).crossed_observations += 1
            if reason == REASON_QUOTABLE:
                _sample(_stats(report, asset_id), book)
            digest.update(_digest_line(sequence, asset_id, book, reason).encode("utf-8"))

    report.digest = digest.hexdigest()
    _add_warnings(report)
    return report


class _StateClock:
    """Accumulate, per asset, the time spent quotable and the time spent rejected."""

    def __init__(self, books: dict[str, OrderBook], report: ReplayReport, max_stale_ms: int) -> None:
        self._books = books
        self._report = report
        self._max_stale_ms = max_stale_ms
        self._last_ms: int | None = None

    def advance(self, now_ms: int | None) -> None:
        if now_ms is None:
            return
        if self._last_ms is None:
            self._last_ms = now_ms
            return
        if now_ms <= self._last_ms:
            return
        start, end = self._last_ms, now_ms
        for asset_id, book in self._books.items():
            stats = self._report.assets.setdefault(asset_id, AssetStats())
            # A fresh book can go stale part-way through the interval; split there.
            boundary = book.stale_at_ms(self._max_stale_ms)
            first_reason = book.reject_reason(start, self._max_stale_ms)
            if (
                first_reason == REASON_QUOTABLE
                and boundary is not None
                and start < boundary < end
            ):
                _add_ms(stats, REASON_QUOTABLE, boundary - start)
                _add_ms(stats, REASON_STALE, end - boundary)
            else:
                _add_ms(stats, first_reason, end - start)
        self._last_ms = now_ms


def _add_ms(stats: AssetStats, reason: str, duration_ms: int) -> None:
    if duration_ms <= 0:
        return
    stats.state_ms[reason] = stats.state_ms.get(reason, 0) + duration_ms


def _book_for(
    books: dict[str, OrderBook],
    report: ReplayReport,
    asset_id: str,
    labels: dict[str, str],
) -> OrderBook:
    if asset_id not in books:
        books[asset_id] = OrderBook()
    stats = report.assets.setdefault(asset_id, AssetStats())
    if not stats.label:
        stats.label = labels.get(asset_id, "")
    return books[asset_id]


def _stats(report: ReplayReport, asset_id: str) -> AssetStats:
    return report.assets.setdefault(asset_id, AssetStats())


def _sample(stats: AssetStats, book: OrderBook) -> None:
    spread = book.spread
    if spread is not None:
        stats.spreads.append(spread)
    bid_depth, ask_depth = book.depth_at_best()
    total_bid, total_ask = book.total_depth()
    stats.best_bid_depths.append(bid_depth)
    stats.best_ask_depths.append(ask_depth)
    stats.total_bid_depths.append(total_bid)
    stats.total_ask_depths.append(total_ask)


def _digest_line(sequence: int, asset_id: str, book: OrderBook, reason: str) -> str:
    bid_depth, ask_depth = book.depth_at_best()
    total_bid, total_ask = book.total_depth()
    parts = [
        str(sequence),
        asset_id,
        _fmt(book.best_bid),
        _fmt(book.best_ask),
        str(len(book.bids)),
        str(len(book.asks)),
        _fmt(bid_depth),
        _fmt(ask_depth),
        _fmt(total_bid),
        _fmt(total_ask),
        reason,
    ]
    return "|".join(parts) + "\n"


def _add_warnings(report: ReplayReport) -> None:
    for asset_id, stats in sorted(report.assets.items()):
        name = stats.label or asset_id[:12]
        quotable_ms = stats.state_ms.get(REASON_QUOTABLE, 0)
        total_ms = sum(stats.state_ms.values())
        if total_ms and quotable_ms / total_ms < 0.9:
            report.warnings.append(
                f"{name}: quotable only {quotable_ms / total_ms:.1%} of the recording"
            )
        if stats.orphan_price_changes:
            report.warnings.append(
                f"{name}: {stats.orphan_price_changes} price changes arrived before any snapshot"
            )
        if stats.crossed_observations:
            report.warnings.append(
                f"{name}: book was crossed on {stats.crossed_observations} updates"
            )
        if stats.top_of_book_mismatches:
            report.warnings.append(
                f"{name}: local top of book disagreed with the venue's in-band top on "
                f"{stats.top_of_book_mismatches} updates"
            )
        if not stats.snapshots:
            report.warnings.append(f"{name}: no book snapshot was ever recorded")
    unclosed = [gap for gap in report.gaps if gap.closed_at_ms is None]
    if unclosed:
        report.warnings.append(f"{len(unclosed)} data gap(s) never closed before the recording ended")
    if report.out_of_order_exchange_timestamps:
        report.warnings.append(
            f"{report.out_of_order_exchange_timestamps} events arrived with a backwards exchange timestamp"
        )


def _asset_to_dict(stats: AssetStats) -> dict[str, Any]:
    total_ms = sum(stats.state_ms.values())
    quotable_ms = stats.state_ms.get(REASON_QUOTABLE, 0)
    return {
        "label": stats.label,
        "counts": {
            "snapshots": stats.snapshots,
            "price_changes": stats.price_changes,
            "trades": stats.trades,
            "tick_size_changes": stats.tick_size_changes,
            "orphan_price_changes": stats.orphan_price_changes,
            "crossed_observations": stats.crossed_observations,
            "top_of_book_mismatches": stats.top_of_book_mismatches,
            "best_bid_ask_disagreements": stats.best_bid_ask_disagreements,
            "best_bid_ask_checks": stats.best_bid_ask_checks,
        },
        "quotable_fraction": None if not total_ms else round(quotable_ms / total_ms, 4),
        "state_seconds": {
            reason: round(stats.state_ms.get(reason, 0) / 1000, 3)
            for reason in (REASON_QUOTABLE, *REJECT_REASONS)
            if stats.state_ms.get(reason)
        },
        "spread": _summarize(stats.spreads),
        "depth_at_best_bid": _summarize(stats.best_bid_depths),
        "depth_at_best_ask": _summarize(stats.best_ask_depths),
        "total_depth_bids": _summarize(stats.total_bid_depths),
        "total_depth_asks": _summarize(stats.total_ask_depths),
    }


def _summarize(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"samples": 0, "min": None, "median": None, "mean": None, "max": None}
    return {
        "samples": len(values),
        "min": round(min(values), 6),
        "median": round(statistics.median(values), 6),
        "mean": round(statistics.fmean(values), 6),
        "max": round(max(values), 6),
    }


def load_recording(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load a recording from its directory or its events file."""
    directory = path if path.is_dir() else path.parent
    events_path = path / EVENTS_FILENAME if path.is_dir() else path
    metadata_path = directory / METADATA_FILENAME
    metadata: dict[str, Any] = {}
    if metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    return load_events(events_path), metadata


def load_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def labels_from_metadata(metadata: dict[str, Any]) -> dict[str, str]:
    """Map token ID -> outcome label exactly as the market published it."""
    market = metadata.get("market") or {}
    return {
        str(outcome.get("token_id")): str(outcome.get("label") or "")
        for outcome in market.get("outcomes") or []
        if outcome.get("token_id")
    }


def _row_time_ms(row: dict[str, Any]) -> int | None:
    value = row.get("received_unix_ms")
    if value is not None:
        return int(value)
    value = row.get("exchange_timestamp_ms")
    return int(value) if value is not None else None


def _optional_price(value: Any) -> float | None:
    """Parse a venue-supplied price string, or None when it is absent."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _levels(rows: list[dict[str, str]]) -> dict[str, float]:
    return {str(row["price"]): float(row["size"]) for row in rows if float(row["size"]) > 0}


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.6f}"


@dataclass(frozen=True)
class Trade:
    """A trade the venue reported. `side` is the taker's side."""

    asset_id: str
    price: float
    size: float
    side: str


@dataclass
class Tick:
    """One recorded event, with book state as of immediately after it."""

    sequence: int
    at_ms: int | None
    kind: str
    event_type: str
    payload: dict[str, Any]
    books: dict[str, OrderBook]
    trades: list[Trade]
    gap_open: bool

    def quotable(self, asset_id: str, max_stale_ms: int = DEFAULT_MAX_STALE_MS) -> bool:
        book = self.books.get(asset_id)
        if book is None:
            return False
        return book.reject_reason(self.at_ms, max_stale_ms) == REASON_QUOTABLE


def walk(
    rows: Iterable[dict[str, Any]],
    *,
    divergence_tolerance: int = DEFAULT_DIVERGENCE_TOLERANCE,
) -> Iterable[Tick]:
    """Yield each recorded event with the reconstructed books after applying it.

    The shared primitive behind both the replay report and the simulator, so book
    mechanics are written once. Broadcast events are skipped.
    """
    books: dict[str, OrderBook] = {}
    gap_open = False

    for index, row in enumerate(rows, start=1):
        kind = str(row.get("kind", ""))
        payload = row.get("payload") or {}
        sequence = int(row.get("sequence") or index)
        at_ms = _row_time_ms(row)

        if kind == "collector_gap":
            state = str(payload.get("state", ""))
            if state == "opened":
                gap_open = True
                for book in books.values():
                    book.invalidate(REASON_GAP_OPEN)
            elif state == "closed":
                gap_open = False
            continue
        if kind != "market_event":
            continue

        event_type = str(payload.get("event_type") or "")
        if event_type in BROADCAST_EVENT_TYPES or event_type not in BOOK_EVENT_TYPES:
            continue

        trades: list[Trade] = []
        venue_top: dict[str, tuple[Any, Any]] = {}

        if event_type == "book":
            asset_id = str(payload["asset_id"])
            books.setdefault(asset_id, OrderBook()).apply_snapshot(
                payload, sequence=sequence, at_ms=at_ms
            )
        elif event_type == "price_change":
            for change in payload.get("price_changes") or []:
                asset_id = str(change["asset_id"])
                book = books.setdefault(asset_id, OrderBook())
                if not book.has_snapshot:
                    book.invalidate(REASON_NO_SNAPSHOT)
                    continue
                book.apply_price_change(change, at_ms=at_ms)
                venue_top[asset_id] = (change.get("best_bid"), change.get("best_ask"))
        elif event_type == "last_trade_price":
            asset_id = str(payload.get("asset_id") or "")
            books.setdefault(asset_id, OrderBook())
            trades.append(Trade(
                asset_id=asset_id,
                price=float(payload.get("price") or 0.0),
                size=float(payload.get("size") or 0.0),
                side=str(payload.get("side") or ""),
            ))

        for asset_id, (venue_bid, venue_ask) in venue_top.items():
            books[asset_id].check_against_venue_top(
                venue_bid, venue_ask, tolerance=divergence_tolerance
            )

        yield Tick(
            sequence=sequence, at_ms=at_ms, kind=kind, event_type=event_type,
            payload=payload, books=books, trades=trades, gap_open=gap_open,
        )
