"""Signal generation — filled in during Step 4 of the tutorial.

This module turns the bot's price feed observations and the current market
order book into a trading decision: BUY UP, BUY DOWN, or do nothing.

The example below is a *starting point* — a simple momentum signal. The
tutorial walks through extending it with spread filters, time-to-expiry
filters, and confidence-based sizing.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.price_feed import get_price_delta_pct
from src.utils import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Signal:
    direction: str          # 'UP' or 'DOWN'
    confidence: float       # 0.0 to 1.0
    up_mid: float           # current UP-share market mid price
    price_delta_pct: float  # BTC % change over signal lookback window


MOMENTUM_THRESHOLD_PCT = 0.15
EDGE_THRESHOLD = 0.05
MIN_CONFIDENCE = 0.55


def generate_signal(up_mid: float) -> Signal | None:
    """Return a Signal or None. None means 'no edge — skip this cycle'.

    `up_mid` is the current mid price of the UP share on the CLOB. A higher
    `up_mid` means the market thinks UP is more likely.
    """
    delta = get_price_delta_pct(lookback_seconds=900)
    if delta is None:
        logger.info("Insufficient price history — skipping signal")
        return None

    if delta > MOMENTUM_THRESHOLD_PCT:
        # BTC rising — UP is more likely than the market implies
        implied_up_prob = up_mid
        our_up_prob = min(0.95, implied_up_prob + abs(delta) * 0.1)
        edge = our_up_prob - implied_up_prob
        if edge >= EDGE_THRESHOLD:
            confidence = min(1.0, 0.5 + edge)
            if confidence >= MIN_CONFIDENCE:
                return Signal("UP", confidence, up_mid, delta)

    elif delta < -MOMENTUM_THRESHOLD_PCT:
        # BTC falling — DOWN is more likely than the market implies
        implied_down_prob = 1.0 - up_mid
        our_down_prob = min(0.95, implied_down_prob + abs(delta) * 0.1)
        edge = our_down_prob - implied_down_prob
        if edge >= EDGE_THRESHOLD:
            confidence = min(1.0, 0.5 + edge)
            if confidence >= MIN_CONFIDENCE:
                return Signal("DOWN", confidence, up_mid, delta)

    logger.info(f"No edge detected. delta={delta:.3f}%, up_mid={up_mid:.3f}")
    return None
