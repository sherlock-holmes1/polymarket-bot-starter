"""Order placement & management — filled in during Step 5 of the tutorial.

Wraps `py_clob_client_v2` to build and submit BUY limit orders for either the
UP or DOWN token of the active BTC Up/Down 15M market, and to poll/cancel.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone

from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import OrderArgs
from py_clob_client_v2.order_builder.constants import BUY

from src.markets import ActiveMarket
from src.signal_engine import Signal
from src.utils import get_logger

logger = get_logger(__name__)

PAPER_TRADING = os.getenv("PAPER_TRADING", "false").lower() == "true"


def round_to_tick(price: float, tick: float = 0.01) -> float:
    """Round price DOWN to the nearest valid tick. Polymarket rejects off-tick prices."""
    return round(math.floor(price / tick) * tick, 10)


def place_limit_order(
    client: ClobClient,
    signal: Signal,
    market: ActiveMarket,
    size_pusd: float,
) -> dict | None:
    """Place a BUY limit order in the direction of `signal`.

    Returns the order response dict, or None on failure.
    """
    token_id = market.up_token_id if signal.direction == "UP" else market.down_token_id

    # For UP: pay at or just above the UP ask. For DOWN: the DOWN price is (1 - up_mid).
    if signal.direction == "UP":
        limit_price = round_to_tick(signal.up_mid + 0.01, market.tick_size)
    else:
        limit_price = round_to_tick((1.0 - signal.up_mid) + 0.01, market.tick_size)
    limit_price = min(limit_price, 0.99)

    if PAPER_TRADING:
        return _paper_fill(signal, market, size_pusd, limit_price)

    order_args = OrderArgs(
        token_id=token_id,
        price=limit_price,
        size=round(size_pusd, 2),
        side=BUY,
    )
    try:
        resp = client.create_and_post_order(order_args)
        logger.info(
            f"Order placed: {signal.direction} @ {limit_price} size={size_pusd} "
            f"order_id={resp.get('orderID')}"
        )
        return resp
    except Exception as exc:
        logger.error(f"Order placement failed: {exc!r}")
        return None


def _paper_fill(signal: Signal, market: ActiveMarket, size_pusd: float, limit_price: float) -> dict:
    """Simulated fill for paper-trading mode. Filled in fully in Step 8."""
    order_id = "PAPER-" + datetime.now(timezone.utc).strftime("%H%M%S%f")[:12]
    logger.info(
        f"[PAPER] {signal.direction} @ {limit_price} size={size_pusd} "
        f"market={market.market_slug}"
    )
    return {"orderID": order_id, "status": "FILLED", "fill_price": limit_price}
