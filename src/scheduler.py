"""Main trading loop — filled in during Step 7 of the tutorial.

Runs one trading cycle per active market window:
  1. Look up the currently active BTC Up/Down 15M market
  2. Pull current UP-share mid price from the order book
  3. Ask the signal engine for a direction
  4. Ask the risk manager for a size + a go/no-go
  5. Place the order, monitor for fill, log the result

The scheduler is structured so it always starts a new cycle at the START of a
new market window (using `end_date_iso` to compute the wait), rather than at
fixed wall-clock offsets from process start. The 15M cycle is the natural unit
of time for this bot, so the loop tracks it directly instead of fighting it.
"""
from __future__ import annotations

import time

from py_clob_client_v2 import ClobClient

from src.markets import get_active_btc_15m_market
from src.risk import RiskManager
from src.utils import get_logger

logger = get_logger(__name__)


def run_trading_cycle(client: ClobClient, risk: RiskManager) -> None:
    """Single end-to-end cycle. Filled in during Step 7."""
    try:
        market = get_active_btc_15m_market(client)
    except RuntimeError as exc:
        logger.warning(f"No active market this cycle: {exc}")
        return

    logger.info(
        f"Cycle start: {market.market_slug} expires in {market.seconds_to_expiry}s"
    )
    # Step 4 fills in: read up_mid, call generate_signal()
    # Step 5 fills in: place_limit_order(...)
    # Step 6 fills in: risk gate before placing
    # Step 7 wires it all together here.


def run_forever(client: ClobClient, risk: RiskManager) -> None:
    """Run trading cycles forever, aligned to the 15M market boundary."""
    while True:
        run_trading_cycle(client, risk)
        try:
            market = get_active_btc_15m_market(client)
            wait_s = max(5, market.seconds_to_expiry + 5)
        except Exception as exc:
            logger.warning(f"Cycle wait fallback to 60s — lookup failed: {exc!r}")
            wait_s = 60
        logger.info(f"Sleeping {wait_s}s until next cycle boundary")
        time.sleep(wait_s)
