"""Collector bookkeeping: market spec capture and data-gap flagging.

The collector is driven directly here — no sockets, no network. What matters is
that the chosen market's full configuration lands in the recording with its
observation timestamp, and that a dropped connection opens a gap that only closes
once a fresh snapshot has arrived for every subscribed token.
"""
from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import src.collector as collector_module
from src.collector import PublicMarketCollector
from src.market_spec import build_market_spec

UP = "up-token"
DOWN = "down-token"

MARKET = {
    "condition_id": "0xabc",
    "question_id": "0xdef",
    "question": "Bitcoin Up or Down - September 13, 3:15PM-3:30PM ET",
    "market_slug": "btc-updown-15m-1789326900",
    "end_date_iso": "2026-09-13T00:00:00Z",
    "active": True,
    "closed": False,
    "accepting_orders": True,
    "accepting_order_timestamp": "2026-09-12T19:56:36Z",
    "enable_order_book": True,
    "minimum_tick_size": 0.01,
    "minimum_order_size": 5,
    "maker_base_fee": 1000,
    "taker_base_fee": 1000,
    "neg_risk": False,
    "tags": ["Crypto", "15M"],
    "rewards": {"rates": None, "min_size": 50, "max_spread": 4.5},
    "tokens": [
        {"token_id": UP, "outcome": "Up", "price": 0.355, "winner": False},
        {"token_id": DOWN, "outcome": "Down", "price": 0.645, "winner": False},
    ],
}


def _collector(tmp_path: Path) -> PublicMarketCollector:
    spec = build_market_spec(MARKET)
    return PublicMarketCollector(tmp_path, lambda: spec, mode="fixed", spec_refresh_s=3600)


def _rows(collector: PublicMarketCollector) -> list[dict]:
    path = collector.recording_directory / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_full_market_configuration_is_recorded_with_its_observation_time(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector._active_token_ids()

    metadata = json.loads((collector.recording_directory / "metadata.json").read_text())
    market = metadata["market"]
    assert market["minimum_tick_size"] == 0.01
    assert market["minimum_order_size"] == 5.0
    assert market["taker_base_fee"] == 1000
    assert market["rewards"] == {"rates": [], "min_size": 50.0, "max_spread": 4.5}
    assert [outcome["label"] for outcome in market["outcomes"]] == ["Up", "Down"]
    assert market["observed_at"].endswith("Z")
    assert "raw" not in market  # the untouched payload lives in the event stream

    spec_event = _rows(collector)[0]
    assert spec_event["kind"] == "market_spec"
    assert spec_event["payload"]["raw"]["condition_id"] == "0xabc"


def test_outcome_labels_are_kept_exactly_as_the_market_supplies_them(tmp_path: Path) -> None:
    spec = build_market_spec({**MARKET, "tokens": [
        {"token_id": UP, "outcome": "Yes", "price": 0.5, "winner": False},
        {"token_id": DOWN, "outcome": "No", "price": 0.5, "winner": False},
    ]})
    assert spec.labels_by_token_id == {UP: "Yes", DOWN: "No"}


def test_a_disconnect_opens_a_gap_that_only_a_full_snapshot_set_closes(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector.start = lambda: None  # type: ignore[method-assign]
    collector._active_token_ids()

    collector._on_connection_state("subscribed", {"token_ids": [UP, DOWN], "generation": 1})
    collector._on_market_event({"event_type": "book", "asset_id": UP})
    collector._on_market_event({"event_type": "book", "asset_id": DOWN})
    collector._on_connection_state("disconnected", {"status_code": 1006, "generation": 1})
    collector._on_market_event({"event_type": "book", "asset_id": UP})

    gaps = [row for row in _rows(collector) if row["kind"] == "collector_gap"]
    assert [gap["payload"]["state"] for gap in gaps] == ["opened"]

    collector._on_market_event({"event_type": "book", "asset_id": DOWN})
    gaps = [row for row in _rows(collector) if row["kind"] == "collector_gap"]
    assert [gap["payload"]["state"] for gap in gaps] == ["opened", "closed"]
    assert gaps[1]["payload"]["recovered_by"] == "book_snapshot"


def test_repeated_disconnects_do_not_open_overlapping_gaps(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector._active_token_ids()
    collector._on_connection_state("subscribed", {"token_ids": [UP, DOWN], "generation": 1})
    collector._on_connection_state("disconnected", {"status_code": 1006, "generation": 1})
    collector._on_connection_state("disconnected", {"status_code": 1006, "generation": 2})

    gaps = [row for row in _rows(collector) if row["kind"] == "collector_gap"]
    assert [gap["payload"]["state"] for gap in gaps] == ["opened"]


def test_a_gap_still_open_at_shutdown_is_closed_out_in_the_recording(tmp_path: Path) -> None:
    collector = _collector(tmp_path)
    collector._active_token_ids()
    collector._on_connection_state("subscribed", {"token_ids": [UP, DOWN], "generation": 1})
    collector._on_connection_state("disconnected", {"status_code": 1006, "generation": 1})
    collector.stop()

    kinds = [(row["kind"], row["payload"].get("state")) for row in _rows(collector)]
    assert ("collector_gap", "opened") in kinds
    assert ("collector_gap", "closed") in kinds
    assert kinds[-1][0] == "collector_stopped"


def test_rolling_5m_selection_refreshes_when_the_slug_rolls(monkeypatch) -> None:
    spec = build_market_spec({**MARKET, "market_slug": "btc-updown-5m-1789843500"})
    monkeypatch.setattr(collector_module, "select_active_btc_updown_5m", lambda _client: spec)
    monkeypatch.setattr(collector_module, "current_btc_updown_5m_slug", lambda: spec.market_slug)
    args = Namespace(condition_id=None, slug=None, btc_updown_15m=False, btc_updown_5m=True)

    resolve, mode, rolled = collector_module._build_resolver(object(), args)

    assert resolve() is spec
    assert mode == "btc-updown-5m"
    assert rolled(spec) is False
    monkeypatch.setattr(collector_module, "current_btc_updown_5m_slug", lambda: "btc-updown-5m-1789843800")
    assert rolled(spec) is True
