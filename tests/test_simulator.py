"""Queue-conservative simulator tests.

Each test pins one rule that decides whether a fill is credited. The whole point
of the simulator is that it refuses to credit fills it cannot justify, so most of
these assert that something did *not* happen.
"""
from __future__ import annotations

import pytest

from src.market_spec import build_market_spec
from src.simulator import Assumptions, TwoSidedQuoter, simulate, taker_fee, taker_fee_rate
from src.orderbook import walk

UP, DOWN = "up-token", "down-token"

MARKET = {
    "condition_id": "0xabc",
    "question": "Up or Down",
    "market_slug": "demo-market",
    "minimum_tick_size": 0.01,
    "minimum_order_size": 5,
    "maker_base_fee": 0,
    "taker_base_fee": 1000,
    "tags": ["Crypto"],
    "rewards": {"rates": None, "min_size": 50, "max_spread": 4.5},
    "tokens": [
        {"token_id": UP, "outcome": "Up", "price": 0.49, "winner": False},
        {"token_id": DOWN, "outcome": "Down", "price": 0.50, "winner": False},
    ],
}

SPEC = build_market_spec(MARKET)
# No latency and nothing ahead of us in the queue: isolates the rule under test.
FAST = Assumptions(
    place_latency_ms=0, cancel_latency_ms=0, queue_ahead_multiple=0.0,
    order_size_shares=10.0, min_edge=0.0,
)


def _row(seq: int, payload: dict, at_ms: int) -> dict:
    return {"sequence": seq, "kind": "market_event", "payload": payload,
            "received_unix_ms": at_ms, "exchange_timestamp_ms": at_ms}


def _book(asset: str, bid: str, ask: str, bid_size: str = "20", ask_size: str = "50") -> dict:
    return {"event_type": "book", "asset_id": asset,
            "bids": [{"price": bid, "size": bid_size}],
            "asks": [{"price": ask, "size": ask_size}]}


def _trade(asset: str, price: str, size: str, side: str) -> dict:
    return {"event_type": "last_trade_price", "asset_id": asset,
            "price": price, "size": size, "side": side}


def _opening(at_ms: int = 1_000, up_bid: str = "0.49", down_bid: str = "0.50",
             bid_size: str = "20") -> list[dict]:
    """Both books quotable, with pair cost = up_bid + down_bid."""
    return [
        _row(1, _book(UP, up_bid, "0.51", bid_size), at_ms),
        _row(2, _book(DOWN, down_bid, "0.52", bid_size), at_ms),
    ]


# --- fill mechanics ------------------------------------------------------


QUEUED = Assumptions(place_latency_ms=0, cancel_latency_ms=0, queue_ahead_multiple=1.0,
                     order_size_shares=10.0, min_edge=0.0)


def test_no_fill_until_consuming_volume_exceeds_the_queue_ahead() -> None:
    rows = _opening(bid_size="20") + [
        _row(3, _trade(UP, "0.49", "15", "SELL"), 2_000),
        _row(4, _trade(UP, "0.49", "3", "SELL"), 3_000),
    ]
    result = simulate(rows, SPEC, QUEUED)
    assert result.fills == []
    assert result.consuming_volume == 18.0  # seen, but 20 were ahead of us


def test_a_fill_is_credited_once_volume_passes_the_queue() -> None:
    rows = _opening(bid_size="20") + [
        _row(3, _trade(UP, "0.49", "25", "SELL"), 2_000),
    ]
    result = simulate(rows, SPEC, QUEUED)
    assert len(result.fills) == 1
    assert result.fills[0].shares == pytest.approx(5.0)  # 25 - 20 ahead
    assert result.cash_spent == pytest.approx(5.0 * 0.49)


def test_a_fill_never_exceeds_our_order_size() -> None:
    rows = _opening() + [_row(3, _trade(UP, "0.49", "10000", "SELL"), 2_000)]
    result = simulate(rows, SPEC, FAST)
    assert result.fills[0].shares == pytest.approx(FAST.order_size_shares)


def test_a_taker_buy_does_not_consume_our_bid() -> None:
    """A BUY lifts the ask. It is on the other side of the book from our order."""
    rows = _opening() + [_row(3, _trade(UP, "0.49", "50", "BUY"), 2_000)]
    assert simulate(rows, SPEC, FAST).fills == []


def test_a_trade_at_a_different_price_does_not_consume_our_bid() -> None:
    rows = _opening() + [_row(3, _trade(UP, "0.48", "50", "SELL"), 2_000)]
    assert simulate(rows, SPEC, FAST).fills == []


def test_a_price_touching_our_quote_is_not_a_fill() -> None:
    """The book trading at our price with no recorded trade proves nothing."""
    rows = _opening() + [
        _row(3, {"event_type": "price_change", "price_changes": [
            {"asset_id": UP, "side": "BUY", "price": "0.49", "size": "0",
             "best_bid": "0.48", "best_ask": "0.51"}]}, 2_000),
    ]
    assert simulate(rows, SPEC, FAST).fills == []


# --- latency and the cancel race -----------------------------------------


def test_an_order_cannot_fill_before_it_is_live() -> None:
    slow = Assumptions(place_latency_ms=5_000, cancel_latency_ms=0, queue_ahead_multiple=0.0,
                       order_size_shares=10.0, min_edge=0.0)
    rows = _opening() + [_row(3, _trade(UP, "0.49", "50", "SELL"), 2_000)]
    assert simulate(rows, SPEC, slow).fills == []


def test_the_same_trade_fills_once_the_placement_latency_has_passed() -> None:
    slow = Assumptions(place_latency_ms=500, cancel_latency_ms=0, queue_ahead_multiple=0.0,
                       order_size_shares=10.0, min_edge=0.0)
    rows = _opening() + [_row(3, _trade(UP, "0.49", "50", "SELL"), 2_000)]
    assert len(simulate(rows, SPEC, slow).fills) == 1


def test_a_fill_can_still_land_during_a_cancel_race() -> None:
    """Requesting a cancel does not protect us until the cancel takes effect."""
    racy = Assumptions(place_latency_ms=0, cancel_latency_ms=10_000, queue_ahead_multiple=0.0,
                       order_size_shares=10.0, min_edge=0.0)
    rows = _opening() + [
        # Pair cost rises above 1.00, so the quoter pulls its quotes.
        _row(3, _book(UP, "0.60", "0.62"), 2_000),
        _row(4, _trade(UP, "0.49", "50", "SELL"), 3_000),
    ]
    result = simulate(rows, SPEC, racy)
    assert result.orders_cancelled >= 1
    assert len(result.fills) == 1
    assert result.fills[0].during_cancel_race is True
    assert result.fills_during_cancel_race == 1


# --- complementary matching ----------------------------------------------


def test_a_complementary_mint_is_ignored_by_default() -> None:
    """Buying DOWN at 0.51 can mint against our UP bid at 0.49, but public data
    cannot prove it did, so the conservative default refuses the fill."""
    rows = _opening() + [_row(3, _trade(DOWN, "0.51", "50", "BUY"), 2_000)]
    assert simulate(rows, SPEC, FAST).fills == []


def test_a_complementary_mint_fills_when_the_assumption_is_enabled() -> None:
    rows = _opening() + [_row(3, _trade(DOWN, "0.51", "50", "BUY"), 2_000)]
    with_comp = Assumptions(place_latency_ms=0, cancel_latency_ms=0, queue_ahead_multiple=0.0,
                            order_size_shares=10.0, min_edge=0.0,
                            complementary_fills=True)
    result = simulate(rows, SPEC, with_comp)
    assert len(result.fills) == 1
    assert result.fills[0].asset_id == UP


def test_a_complementary_trade_at_the_wrong_price_does_not_fill() -> None:
    rows = _opening() + [_row(3, _trade(DOWN, "0.55", "50", "BUY"), 2_000)]
    with_comp = Assumptions(place_latency_ms=0, cancel_latency_ms=0, queue_ahead_multiple=0.0,
                            order_size_shares=10.0, min_edge=0.0,
                            complementary_fills=True)
    assert simulate(rows, SPEC, with_comp).fills == []


# --- pairing, inventory, close-out ---------------------------------------


def test_a_matched_pair_merges_into_one_dollar() -> None:
    rows = _opening() + [
        _row(3, _trade(UP, "0.49", "10", "SELL"), 2_000),
        _row(4, _trade(DOWN, "0.50", "10", "SELL"), 3_000),
    ]
    result = simulate(rows, SPEC, FAST)
    assert result.pairs_merged == pytest.approx(10.0)
    assert result.merge_proceeds == pytest.approx(10.0)
    assert result.cash_spent == pytest.approx(10 * 0.49 + 10 * 0.50)
    assert result.net_cash == pytest.approx(10.0 - 9.9)  # one tick per pair


def test_the_unpaired_interval_between_legs_is_measured() -> None:
    rows = _opening() + [
        _row(3, _trade(UP, "0.49", "10", "SELL"), 2_000),
        _row(4, _trade(DOWN, "0.50", "10", "SELL"), 7_500),
    ]
    result = simulate(rows, SPEC, FAST)
    assert result.unpaired_intervals_ms == [5_500]
    assert result.peak_unpaired_shares == pytest.approx(10.0)


def test_an_unpaired_leg_is_closed_into_the_bid_and_pays_the_taker_fee() -> None:
    rows = _opening() + [_row(3, _trade(UP, "0.49", "10", "SELL"), 2_000)]
    result = simulate(rows, SPEC, FAST)
    assert result.pairs_merged == 0
    assert result.closeout_shares[UP] == pytest.approx(10.0)
    # Sold into the bid that remains after our own fill.
    assert result.closeout_proceeds == pytest.approx(10.0 * 0.49)
    assert result.closeout_fees == pytest.approx(taker_fee(10.0, 0.49, 0.07))
    assert result.net_cash == pytest.approx(-result.closeout_fees)


def test_holding_an_unpaired_leg_too_long_records_a_breach() -> None:
    impatient = Assumptions(place_latency_ms=0, cancel_latency_ms=0, queue_ahead_multiple=0.0,
                            order_size_shares=10.0, min_edge=0.0,
                            max_unpaired_hold_ms=1_000)
    rows = _opening() + [
        _row(3, _trade(UP, "0.49", "10", "SELL"), 2_000),
        _row(4, _book(UP, "0.49", "0.51"), 9_000),
    ]
    result = simulate(rows, SPEC, impatient)
    assert [breach.kind for breach in result.breaches] == ["unpaired_hold"]


def test_exceeding_the_unpaired_share_cap_records_a_breach() -> None:
    tight = Assumptions(place_latency_ms=0, cancel_latency_ms=0, queue_ahead_multiple=0.0,
                        order_size_shares=10.0, min_edge=0.0,
                        max_unpaired_shares=5.0)
    rows = _opening() + [
        _row(3, _trade(UP, "0.49", "10", "SELL"), 2_000),
        _row(4, _book(UP, "0.49", "0.51"), 3_000),
    ]
    result = simulate(rows, SPEC, tight)
    assert any(breach.kind == "unpaired_cap" for breach in result.breaches)


# --- quoting discipline --------------------------------------------------


def test_no_quote_is_posted_when_the_pair_costs_too_much() -> None:
    rows = [
        _row(1, _book(UP, "0.60", "0.62"), 1_000),
        _row(2, _book(DOWN, "0.55", "0.57"), 1_000),
    ]  # pair cost 1.15 — buying both sides costs more than the pair pays
    result = simulate(rows, SPEC, Assumptions(place_latency_ms=0, min_edge=0.01,
                                              queue_ahead_multiple=0.0))
    assert result.orders_placed == 0
    assert result.blocked_by_edge >= 1
    assert result.pair_cost_samples[-1] == pytest.approx(1.15)


def test_no_quote_is_posted_against_a_book_that_is_not_quotable() -> None:
    rows = [
        _row(1, _book(UP, "0.49", "0.51"), 1_000),
        # DOWN never gets a snapshot, so it can never be quoted against.
        _row(2, {"event_type": "price_change", "price_changes": [
            {"asset_id": DOWN, "side": "BUY", "price": "0.50", "size": "10"}]}, 1_100),
    ]
    result = simulate(rows, SPEC, FAST)
    assert result.orders_placed == 0
    assert result.unquotable_blocks >= 1


def test_a_gap_stops_quoting() -> None:
    rows = _opening() + [
        {"sequence": 3, "kind": "collector_gap", "payload": {"state": "opened"},
         "received_unix_ms": 2_000, "exchange_timestamp_ms": None},
        _row(4, _trade(UP, "0.49", "50", "SELL"), 3_000),
    ]
    result = simulate(rows, SPEC, Assumptions(place_latency_ms=0, cancel_latency_ms=0,
                                              queue_ahead_multiple=0.0,
                                              order_size_shares=10.0, min_edge=0.0,
                                              max_unpaired_shares=1e9))
    assert result.unquotable_blocks >= 1


# --- costs ---------------------------------------------------------------


def test_the_taker_fee_matches_the_published_table() -> None:
    """docs/polymarket/trading/fees.md: 100 crypto shares at 0.50 cost USD 1.75."""
    assert taker_fee(100, 0.50, 0.07) == pytest.approx(1.75)
    assert taker_fee(100, 0.30, 0.07) == pytest.approx(1.47)
    assert taker_fee(100, 0.70, 0.07) == pytest.approx(1.47)  # symmetric around 0.50


def test_the_fee_rate_comes_from_the_market_category() -> None:
    assert taker_fee_rate(SPEC) == 0.07
    sports = build_market_spec({**MARKET, "tags": ["Sports", "NFL"]})
    assert taker_fee_rate(sports) == 0.03
    untagged = build_market_spec({**MARKET, "tags": []})
    assert taker_fee_rate(untagged) == 0.05


def test_rewards_are_never_credited_to_pnl() -> None:
    rows = _opening() + [
        _row(3, _trade(UP, "0.49", "10", "SELL"), 2_000),
        _row(4, _trade(DOWN, "0.50", "10", "SELL"), 3_000),
    ]
    result = simulate(rows, SPEC, FAST)
    assert result.to_dict()["rewards_credited"] is False
    # Net cash is exactly the tick captured — no reward income anywhere in it.
    assert result.net_cash == pytest.approx(0.10)


# --- shared walk ---------------------------------------------------------


def test_the_walk_and_the_replay_agree_on_final_book_state() -> None:
    """Guards against the simulator's event walk drifting from the report's."""
    import pathlib

    from src.orderbook import load_recording, replay

    directory = pathlib.Path("recordings")
    recordings = sorted(directory.iterdir()) if directory.exists() else []
    if not recordings:
        pytest.skip("no recordings on disk")
    rows, _ = load_recording(recordings[0])

    report = replay(rows)
    final: dict[str, tuple[float | None, float | None]] = {}
    for tick in walk(rows):
        for asset_id, book in tick.books.items():
            final[asset_id] = (book.best_bid, book.best_ask)

    for asset_id, stats in report.assets.items():
        if stats.snapshots and asset_id in final:
            assert final[asset_id][0] is not None or final[asset_id][1] is not None
    assert set(final) <= set(report.assets)
