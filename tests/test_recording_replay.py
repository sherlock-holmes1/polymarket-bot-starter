"""Recorder and replay tests for the read-only market-data milestone.

These cover the properties the milestone actually asserts: both clocks are
recorded, a book is not quotable while stale, crossed, gapped, or un-snapshotted,
gaps are exposed in the report, and replaying the same file twice produces the
same book states.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.orderbook import (
    REASON_CROSSED,
    REASON_DIVERGED,
    REASON_EMPTY_SIDE,
    REASON_GAP_OPEN,
    REASON_NO_SNAPSHOT,
    REASON_QUOTABLE,
    REASON_STALE,
    OrderBook,
    labels_from_metadata,
    load_recording,
    replay,
)
from src.recording import JsonlRecorder

UP = "up-token"
DOWN = "down-token"


def _row(sequence: int, kind: str, payload: dict, *, received_ms: int, exchange_ms: int | None = None) -> dict:
    return {
        "sequence": sequence,
        "kind": kind,
        "payload": payload,
        "received_unix_ms": received_ms,
        "received_at": "2026-09-13T15:00:00Z",
        "received_monotonic_ns": sequence * 1_000_000,
        "exchange_timestamp_ms": exchange_ms,
    }


def _snapshot(asset: str = UP, bid: str = "0.48", ask: str = "0.52") -> dict:
    return {
        "event_type": "book",
        "asset_id": asset,
        "bids": [{"price": bid, "size": "10"}],
        "asks": [{"price": ask, "size": "5"}],
    }


def _price_change(asset: str, side: str, price: str, size: str) -> dict:
    return {
        "event_type": "price_change",
        "price_changes": [{"asset_id": asset, "side": side, "price": price, "size": size}],
    }


# --- recorder ------------------------------------------------------------


def test_recorder_writes_both_clocks_and_the_venue_timestamp(tmp_path: Path) -> None:
    recorder = JsonlRecorder(tmp_path / "rec", {"market": {"market_slug": "demo"}})
    recorder.record("market_event", {"event_type": "book", "asset_id": UP, "timestamp": "1757908892351"})
    recorder.record("market_event", {"event_type": "book", "asset_id": UP})
    recorder.close()

    rows = [json.loads(line) for line in (tmp_path / "rec" / "events.jsonl").read_text().splitlines()]
    assert rows[0]["exchange_timestamp_ms"] == 1757908892351
    assert rows[1]["exchange_timestamp_ms"] is None
    assert rows[0]["received_unix_ms"] > 0
    assert rows[1]["received_monotonic_ns"] >= rows[0]["received_monotonic_ns"]
    assert rows[0]["sequence"] == 1 and rows[1]["sequence"] == 2


def test_recorder_reads_the_venue_timestamp_off_nested_price_changes(tmp_path: Path) -> None:
    recorder = JsonlRecorder(tmp_path / "rec", {})
    recorder.record("market_event", {
        "event_type": "price_change",
        "price_changes": [{"asset_id": UP, "timestamp": "1757908892999", "side": "BUY", "price": "0.5", "size": "1"}],
    })
    recorder.close()
    row = json.loads((tmp_path / "rec" / "events.jsonl").read_text().splitlines()[0])
    assert row["exchange_timestamp_ms"] == 1757908892999


def test_recorder_refuses_to_overwrite_an_existing_recording(tmp_path: Path) -> None:
    JsonlRecorder(tmp_path / "rec", {}).close()
    with pytest.raises(FileExistsError):
        JsonlRecorder(tmp_path / "rec", {})


# --- book mechanics ------------------------------------------------------


def test_snapshot_replaces_state_then_deltas_apply() -> None:
    report = replay([
        _row(1, "market_event", _snapshot(), received_ms=1_000),
        _row(2, "market_event", _price_change(UP, "BUY", "0.49", "12"), received_ms=1_100),
        _row(3, "market_event", _price_change(UP, "SELL", "0.52", "0"), received_ms=1_200),
        _row(4, "market_event", {"event_type": "last_trade_price", "asset_id": UP, "price": "0.49", "size": "2"},
             received_ms=1_300),
    ])
    stats = report.assets[UP]
    assert (stats.snapshots, stats.price_changes, stats.trades) == (1, 2, 1)
    assert report.by_event_type["price_change"] == 2


def test_zero_size_removes_a_level_and_a_missing_side_is_not_quotable() -> None:
    book = OrderBook()
    book.apply_snapshot(_snapshot(), sequence=1, at_ms=1_000)
    book.apply_price_change({"asset_id": UP, "side": "SELL", "price": "0.52", "size": "0"}, at_ms=1_100)
    assert book.asks == {}
    assert book.reject_reason(1_100, 5_000) == REASON_EMPTY_SIDE


def test_unknown_price_change_side_is_rejected_loudly() -> None:
    book = OrderBook()
    book.apply_snapshot(_snapshot(), sequence=1, at_ms=0)
    with pytest.raises(ValueError, match="unknown price-change side"):
        book.apply_price_change({"asset_id": UP, "side": "MAYBE", "price": "0.5", "size": "1"}, at_ms=1)


# --- quotability ---------------------------------------------------------


def test_price_change_before_any_snapshot_is_an_orphan_and_blocks_quoting() -> None:
    report = replay([
        _row(1, "market_event", _price_change(UP, "BUY", "0.49", "12"), received_ms=1_000),
        _row(2, "market_event", _price_change(UP, "BUY", "0.50", "9"), received_ms=1_100),
    ])
    stats = report.assets[UP]
    assert stats.orphan_price_changes == 2
    assert stats.price_changes == 0
    assert stats.state_ms.get(REASON_NO_SNAPSHOT, 0) > 0
    assert any("before any snapshot" in warning for warning in report.warnings)


def test_a_crossed_book_is_counted_and_never_sampled_for_spread() -> None:
    report = replay([
        _row(1, "market_event", _snapshot(), received_ms=1_000),
        _row(2, "market_event", _price_change(UP, "BUY", "0.60", "4"), received_ms=1_100),
        _row(3, "market_event", {"event_type": "last_trade_price", "asset_id": UP}, received_ms=1_200),
    ])
    stats = report.assets[UP]
    assert stats.crossed_observations == 1
    assert stats.spreads == [pytest.approx(0.04)]  # only the pre-cross snapshot was sampled
    assert stats.state_ms.get(REASON_CROSSED, 0) == 100
    assert any("crossed" in warning for warning in report.warnings)


def test_a_gap_invalidates_every_book_until_a_fresh_snapshot_arrives() -> None:
    report = replay([
        _row(1, "market_event", _snapshot(), received_ms=1_000),
        _row(2, "collector_gap", {"state": "opened", "reason": "connection_closed"}, received_ms=2_000),
        _row(3, "market_event", _price_change(UP, "BUY", "0.49", "12"), received_ms=2_500),
        _row(4, "collector_gap", {"state": "closed", "recovered_by": "book_snapshot"}, received_ms=3_000),
        _row(5, "market_event", _snapshot(bid="0.47", ask="0.51"), received_ms=3_100),
        _row(6, "market_event", {"event_type": "last_trade_price", "asset_id": UP}, received_ms=3_200),
    ])
    stats = report.assets[UP]
    assert len(report.gaps) == 1
    assert report.gaps[0].duration_ms == 1_000
    assert report.gaps[0].recovered_by == "book_snapshot"
    # The delta inside the gap is applied but the book stays unquotable until the snapshot.
    assert stats.state_ms.get(REASON_GAP_OPEN, 0) == 1_100
    assert stats.state_ms.get(REASON_QUOTABLE, 0) > 0


def test_a_gap_left_open_at_the_end_is_reported() -> None:
    report = replay([
        _row(1, "market_event", _snapshot(), received_ms=1_000),
        _row(2, "collector_gap", {"state": "opened", "reason": "connection_closed"}, received_ms=2_000),
        _row(3, "collector_stopped", {}, received_ms=2_500),
    ])
    assert report.gaps[0].duration_ms is None
    assert any("never closed" in warning for warning in report.warnings)


def test_a_book_older_than_max_stale_ms_stops_being_quotable_mid_interval() -> None:
    report = replay(
        [
            _row(1, "market_event", _snapshot(), received_ms=1_000, exchange_ms=1_000),
            _row(2, "market_event", {"event_type": "last_trade_price", "asset_id": DOWN}, received_ms=11_000,
                 exchange_ms=11_000),
        ],
        max_stale_ms=2_000,
    )
    stats = report.assets[UP]
    assert stats.state_ms[REASON_QUOTABLE] == 2_000
    assert stats.state_ms[REASON_STALE] == 8_000
    assert any("quotable only" in warning for warning in report.warnings)


# --- report contents -----------------------------------------------------


def test_the_report_carries_duration_counts_spreads_depth_and_latency() -> None:
    report = replay([
        _row(1, "connection", {"state": "subscribed", "token_ids": [UP]}, received_ms=900),
        _row(2, "market_event", _snapshot(), received_ms=1_000, exchange_ms=920),
        _row(3, "market_event", _price_change(UP, "BUY", "0.49", "12"), received_ms=1_400, exchange_ms=1_350),
    ])
    data = report.to_dict()
    assert data["duration_s"] == 0.5
    assert data["by_kind"] == {"connection": 1, "market_event": 2}
    assert data["connection_states"] == {"subscribed": 1}
    # Only the delta contributes: a snapshot timestamp is the last change, not the emit.
    assert data["timestamps"]["exchange_to_receipt_ms"] == {
        "samples": 1, "min": 50, "median": 50, "mean": 50, "max": 50,
    }
    asset = data["quoting"]["assets"][UP]
    assert asset["spread"]["samples"] == 2
    assert asset["spread"]["min"] == pytest.approx(0.03)
    assert asset["depth_at_best_bid"]["max"] == 12
    assert asset["total_depth_asks"]["median"] == 5
    assert data["fills_inferred"] is False


def test_backwards_venue_timestamps_are_counted() -> None:
    report = replay([
        _row(1, "market_event", _snapshot(), received_ms=900),
        _row(2, "market_event", _price_change(UP, "BUY", "0.49", "1"), received_ms=1_000, exchange_ms=5_000),
        _row(3, "market_event", _price_change(UP, "BUY", "0.49", "2"), received_ms=1_100, exchange_ms=4_000),
    ])
    assert report.out_of_order_exchange_timestamps == 1
    assert any("backwards exchange timestamp" in warning for warning in report.warnings)


def test_a_snapshot_timestamp_is_not_treated_as_latency() -> None:
    """`book` carries the last book-changing trade's time, not the emit time."""
    report = replay([
        _row(1, "market_event", _snapshot(), received_ms=20_000, exchange_ms=1_000),
    ])
    assert report.exchange_latency_ms == []
    assert report.out_of_order_exchange_timestamps == 0


def test_venue_wide_broadcasts_do_not_pollute_this_markets_stats() -> None:
    """`new_market` arrives for every market on the venue, not just ours."""
    report = replay([
        _row(1, "market_event", _snapshot(), received_ms=1_000),
        _row(2, "market_event", {"event_type": "new_market", "slug": "xrp-updown-5m-1789413000",
                                 "assets_ids": ["someone-elses-token"]}, received_ms=1_100, exchange_ms=1_050),
    ])
    assert report.broadcast_events == 1
    assert set(report.assets) == {UP}
    assert report.exchange_latency_ms == []


def test_a_resolution_broadcast_is_surfaced_in_the_report() -> None:
    report = replay([
        _row(1, "market_event", _snapshot(), received_ms=1_000),
        _row(2, "market_event", {"event_type": "market_resolved", "slug": "btc-updown-15m-1789326900",
                                 "winning_outcome": "Up", "winning_asset_id": UP}, received_ms=2_000),
    ])
    assert report.resolutions == [{
        "market": None,
        "slug": "btc-updown-15m-1789326900",
        "winning_outcome": "Up",
        "winning_asset_id": UP,
        "at": "1970-01-01T00:00:02Z",
    }]


def test_trades_are_counted_only_from_venue_trade_events() -> None:
    """A recorded price touching a quote is not a fill."""
    report = replay([
        _row(1, "market_event", _snapshot(), received_ms=1_000),
        _row(2, "market_event", _price_change(UP, "SELL", "0.48", "3"), received_ms=1_100),
    ])
    assert report.assets[UP].trades == 0
    assert report.fills_inferred is False


# --- determinism ---------------------------------------------------------


def test_replaying_the_same_rows_twice_produces_the_same_digest() -> None:
    rows = [
        _row(1, "market_event", _snapshot(), received_ms=1_000, exchange_ms=1_000),
        _row(2, "market_event", _price_change(UP, "BUY", "0.49", "12"), received_ms=1_100, exchange_ms=1_090),
        _row(3, "market_event", _snapshot(DOWN, "0.47", "0.51"), received_ms=1_200, exchange_ms=1_190),
        _row(4, "collector_gap", {"state": "opened"}, received_ms=1_300),
    ]
    first, second = replay(rows), replay(list(rows))
    assert first.digest == second.digest
    assert first.to_dict() == second.to_dict()


def test_a_changed_book_state_changes_the_digest() -> None:
    base = [_row(1, "market_event", _snapshot(), received_ms=1_000)]
    changed = [_row(1, "market_event", _snapshot(bid="0.47"), received_ms=1_000)]
    assert replay(base).digest != replay(changed).digest


def test_a_recording_round_trips_through_disk_with_labels(tmp_path: Path) -> None:
    directory = tmp_path / "rec"
    recorder = JsonlRecorder(directory, {
        "market": {
            "market_slug": "btc-updown-15m-1789326900",
            "outcomes": [{"token_id": UP, "label": "Up"}, {"token_id": DOWN, "label": "Down"}],
        }
    })
    recorder.record("market_event", _snapshot())
    recorder.record("market_event", _price_change(UP, "BUY", "0.49", "12"))
    recorder.close()

    rows, metadata = load_recording(directory)
    labels = labels_from_metadata(metadata)
    assert labels == {UP: "Up", DOWN: "Down"}
    report = replay(rows, labels=labels)
    assert report.assets[UP].label == "Up"
    assert replay(load_recording(directory / "events.jsonl")[0], labels=labels).digest == report.digest


# --- consistency against the venue's own top of book ---------------------


def _delta_with_top(asset: str, side: str, price: str, size: str, bid: str, ask: str) -> dict:
    return {
        "event_type": "price_change",
        "price_changes": [{
            "asset_id": asset, "side": side, "price": price, "size": size,
            "best_bid": bid, "best_ask": ask,
        }],
    }


def test_a_single_top_of_book_disagreement_is_a_race_not_a_rejection() -> None:
    report = replay([
        _row(1, "market_event", _snapshot(), received_ms=1_000),
        _row(2, "market_event", _delta_with_top(UP, "BUY", "0.49", "12", "0.50", "0.52"), received_ms=1_100),
        _row(3, "market_event", _delta_with_top(UP, "BUY", "0.49", "13", "0.49", "0.52"), received_ms=1_200),
        _row(4, "market_event", {"event_type": "last_trade_price", "asset_id": UP}, received_ms=1_300),
    ])
    stats = report.assets[UP]
    assert stats.top_of_book_mismatches == 1
    assert stats.state_ms.get(REASON_DIVERGED, 0) == 0
    assert stats.state_ms.get(REASON_QUOTABLE, 0) > 0


def test_sustained_divergence_stops_the_book_being_quotable() -> None:
    """A dropped delta shows up as the venue's top disagreeing, and staying wrong."""
    rows = [_row(1, "market_event", _snapshot(), received_ms=1_000)]
    for index in range(3):
        rows.append(_row(
            2 + index, "market_event",
            _delta_with_top(UP, "BUY", "0.49", str(10 + index), "0.51", "0.52"),
            received_ms=1_100 + index * 100,
        ))
    rows.append(_row(9, "market_event", {"event_type": "last_trade_price", "asset_id": UP}, received_ms=1_500))
    report = replay(rows)

    stats = report.assets[UP]
    assert stats.top_of_book_mismatches == 3
    assert stats.state_ms.get(REASON_DIVERGED, 0) > 0
    assert any("disagreed with the venue" in warning for warning in report.warnings)


def test_a_fresh_snapshot_clears_a_diverged_book() -> None:
    rows = [_row(1, "market_event", _snapshot(), received_ms=1_000)]
    rows += [
        _row(2 + index, "market_event",
             _delta_with_top(UP, "BUY", "0.49", str(10 + index), "0.51", "0.52"),
             received_ms=1_100 + index * 100)
        for index in range(3)
    ]
    rows.append(_row(8, "market_event", _snapshot(bid="0.47", ask="0.51"), received_ms=1_500))
    rows.append(_row(9, "market_event", {"event_type": "last_trade_price", "asset_id": UP}, received_ms=1_600))
    report = replay(rows)

    diverged_ms = report.assets[UP].state_ms.get(REASON_DIVERGED, 0)
    assert 0 < diverged_ms <= 300
    assert report.assets[UP].state_ms.get(REASON_QUOTABLE, 0) > 0


def test_best_bid_ask_is_an_observation_not_a_verdict() -> None:
    """It is sampled at its own instant and disagrees ~31% of the time on live
    data, so it is recorded and reported but never invalidates a book."""
    rows = [_row(1, "market_event", _snapshot(), received_ms=1_000)]
    rows += [
        _row(2 + index, "market_event",
             {"event_type": "best_bid_ask", "asset_id": UP, "best_bid": "0.40", "best_ask": "0.60"},
             received_ms=1_100 + index * 100)
        for index in range(5)
    ]
    rows.append(_row(9, "market_event", {"event_type": "last_trade_price", "asset_id": UP}, received_ms=1_700))
    report = replay(rows)

    stats = report.assets[UP]
    assert stats.best_bid_ask_checks == 5
    assert stats.best_bid_ask_disagreements == 5
    assert stats.top_of_book_mismatches == 0       # never feeds the drift counter
    assert stats.state_ms.get(REASON_DIVERGED, 0) == 0  # five in a row, still quotable
    assert stats.price_changes == 0                # never touches the book
    assert stats.spreads[-1] == pytest.approx(0.04)


def test_best_bid_ask_agreement_is_counted_too() -> None:
    report = replay([
        _row(1, "market_event", _snapshot(), received_ms=1_000),
        _row(2, "market_event", {"event_type": "best_bid_ask", "asset_id": UP,
                                 "best_bid": "0.48", "best_ask": "0.52"}, received_ms=1_100),
    ])
    stats = report.assets[UP]
    assert (stats.best_bid_ask_checks, stats.best_bid_ask_disagreements) == (1, 0)


def test_an_empty_side_matches_the_venues_zero_and_one_convention() -> None:
    report = replay([
        _row(1, "market_event", {"event_type": "book", "asset_id": UP,
                                 "bids": [{"price": "0.48", "size": "10"}], "asks": []}, received_ms=1_000),
        _row(2, "market_event", _delta_with_top(UP, "BUY", "0.48", "11", "0.48", "1"), received_ms=1_100),
    ])
    assert report.assets[UP].top_of_book_mismatches == 0
