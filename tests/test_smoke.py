"""Import smoke test — confirms the starter project's modules are wired correctly."""
from __future__ import annotations


def test_imports() -> None:
    from src import utils, markets, price_feed, market_channel, signal_engine, orders, risk, scheduler  # noqa: F401


def test_round_to_tick() -> None:
    from src.orders import round_to_tick

    assert round_to_tick(0.587, 0.01) == 0.58
    assert round_to_tick(0.99, 0.01) == 0.99
    assert round_to_tick(0.011, 0.01) == 0.01


def test_current_btc_updown_5m_slug_uses_the_five_minute_boundary() -> None:
    from src.market_spec import current_btc_updown_5m_slug

    assert current_btc_updown_5m_slug(1_789_843_799) == "btc-updown-5m-1789843500"
