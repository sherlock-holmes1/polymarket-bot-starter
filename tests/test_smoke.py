"""Import smoke test — confirms the starter project's modules are wired correctly."""
from __future__ import annotations


def test_imports() -> None:
    from src import utils, markets, price_feed, market_channel, signal_engine, orders, risk, scheduler  # noqa: F401


def test_round_to_tick() -> None:
    from src.orders import round_to_tick

    assert round_to_tick(0.587, 0.01) == 0.58
    assert round_to_tick(0.99, 0.01) == 0.99
    assert round_to_tick(0.011, 0.01) == 0.01
