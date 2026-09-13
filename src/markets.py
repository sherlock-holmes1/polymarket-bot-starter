"""Active market lookup for the BTC Up/Down 15M series.

A new BTC Up/Down 15M market opens every 15 minutes with a fresh `condition_id`
and fresh `token_ids` for the UP and DOWN outcomes. The bot must look up the
currently active market on every cycle — hardcoding a condition ID works once
and silently fails on every subsequent cycle.

Two things about this series are easy to get wrong and are handled here:

  * **The slug is `btc-updown-15m-<window-start-epoch>`.** Each window is named
    by its own start time, so the active slug is derivable from the clock. The
    series does not sit near the front of `get_markets()`, so scanning that
    listing finds nothing.
  * **`end_date_iso` on these markets is the calendar day, not the window.** The
    window end is the slug's epoch plus 15 minutes, which is what
    `seconds_to_expiry` uses.

`src/market_spec.py` holds the full public spec (fees, reward config, minimum
order size); this module keeps the narrow view the trading loop needs.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from py_clob_client_v2 import ClobClient

from src.market_spec import (
    BTC_UPDOWN_15M_SLUG_PREFIX,
    BTC_UPDOWN_15M_WINDOW_S,
    MarketSpec,
    select_active_btc_updown_15m,
)
from src.utils import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ActiveMarket:
    condition_id: str
    question: str
    market_slug: str
    end_date_iso: str
    up_token_id: str
    down_token_id: str
    tick_size: float
    minimum_order_size: float = 0.0

    @property
    def seconds_to_expiry(self) -> int:
        end_ts = _parse_iso8601(self.end_date_iso)
        return max(0, int(end_ts - time.time()))


def get_active_btc_15m_market(client: ClobClient) -> ActiveMarket:
    """Return the BTC Up/Down 15M market currently accepting orders.

    Raises RuntimeError between windows — the next market can take a few seconds
    to open, so callers should retry rather than abort the run.
    """
    market = from_spec(select_active_btc_updown_15m(client))
    logger.info(
        f"Active market: {market.market_slug} expires_in={market.seconds_to_expiry}s "
        f"up={market.up_token_id[:10]}… down={market.down_token_id[:10]}…"
    )
    return market


def from_spec(spec: MarketSpec) -> ActiveMarket:
    """Narrow a full `MarketSpec` down to what the trading loop uses."""
    labels = {outcome.label.strip().lower(): outcome.token_id for outcome in spec.outcomes}
    up_id = labels.get("up") or labels.get("yes")
    down_id = labels.get("down") or labels.get("no")
    if not up_id or not down_id:
        raise RuntimeError(
            f"Could not extract Up/Down token IDs from market {spec.condition_id!r}. "
            f"Got outcomes={[outcome.label for outcome in spec.outcomes]!r}"
        )
    return ActiveMarket(
        condition_id=spec.condition_id,
        question=spec.question,
        market_slug=spec.market_slug,
        end_date_iso=window_end_iso(spec),
        up_token_id=up_id,
        down_token_id=down_id,
        tick_size=spec.minimum_tick_size or 0.01,
        minimum_order_size=spec.minimum_order_size,
    )


def window_end_iso(spec: MarketSpec) -> str:
    """End of the 15-minute window, derived from the slug when it is available.

    `end_date_iso` on this series is the calendar day, which would make every
    `seconds_to_expiry` wrong by up to 24 hours.
    """
    suffix = spec.market_slug.removeprefix(f"{BTC_UPDOWN_15M_SLUG_PREFIX}-")
    if suffix.isdigit():
        end = int(suffix) + BTC_UPDOWN_15M_WINDOW_S
        return datetime.fromtimestamp(end, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return spec.end_date_iso


def _parse_iso8601(iso: str) -> float:
    """Lenient ISO-8601 parse that tolerates trailing Z."""
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
