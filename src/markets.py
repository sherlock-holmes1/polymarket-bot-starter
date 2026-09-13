"""Active market lookup for the BTC Up/Down 15M series.

A new BTC Up/Down 15M market opens every 15 minutes with a fresh `condition_id`
and fresh `token_ids` for the UP and DOWN outcomes. The bot must look up the
currently active market on every cycle — hardcoding a condition ID works once
and silently fails on every subsequent cycle.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from py_clob_client_v2 import ClobClient

from src.utils import get_logger, retry_with_backoff

logger = get_logger(__name__)

BTC_15M_SLUG_PREFIX = "btc-up-or-down-15m"


@dataclass(frozen=True)
class ActiveMarket:
    condition_id: str
    question: str
    market_slug: str
    end_date_iso: str
    up_token_id: str
    down_token_id: str
    tick_size: float

    @property
    def seconds_to_expiry(self) -> int:
        end_ts = _parse_iso8601(self.end_date_iso)
        return max(0, int(end_ts - time.time()))


def get_active_btc_15m_market(client: ClobClient) -> ActiveMarket:
    """Return the currently active BTC Up/Down 15M market, or raise RuntimeError.

    Strategy: iterate `client.get_markets(...)` pages, filter to entries whose
    `market_slug` starts with the BTC 15M prefix and that are still `active`
    and not yet `closed`, and pick the one with the soonest `end_date_iso`.
    """

    def _fetch() -> list[dict[str, Any]]:
        # py-clob-client-v2 paginates; we walk pages until we have a hit.
        results: list[dict[str, Any]] = []
        next_cursor: str | None = None
        for _ in range(20):  # hard cap on pages to scan
            page = client.get_markets(next_cursor=next_cursor) if next_cursor else client.get_markets()
            data = page.get("data") or page.get("markets") or []
            results.extend(data)
            next_cursor = page.get("next_cursor")
            if not next_cursor or next_cursor in ("", "LTE="):
                break
        return results

    markets = retry_with_backoff(_fetch, logger=logger)

    candidates: list[dict[str, Any]] = []
    now = time.time()
    for m in markets:
        slug = (m.get("market_slug") or "").lower()
        if not slug.startswith(BTC_15M_SLUG_PREFIX):
            continue
        if not m.get("active") or m.get("closed"):
            continue
        end_iso = m.get("end_date_iso") or ""
        if not end_iso:
            continue
        if _parse_iso8601(end_iso) <= now:
            continue
        candidates.append(m)

    if not candidates:
        raise RuntimeError(
            "No active BTC Up/Down 15M market found. The market briefly disappears between "
            "cycles; retry in 5–10 seconds before giving up on the run."
        )

    candidates.sort(key=lambda m: _parse_iso8601(m["end_date_iso"]))
    pick = candidates[0]

    up_token, down_token = _extract_up_down_tokens(pick)
    am = ActiveMarket(
        condition_id=pick["condition_id"],
        question=pick.get("question", ""),
        market_slug=pick["market_slug"],
        end_date_iso=pick["end_date_iso"],
        up_token_id=up_token,
        down_token_id=down_token,
        tick_size=float(pick.get("minimum_tick_size", 0.01) or 0.01),
    )
    logger.info(
        f"Active market: {am.market_slug} expires_in={am.seconds_to_expiry}s "
        f"up={am.up_token_id[:10]}… down={am.down_token_id[:10]}…"
    )
    return am


def _extract_up_down_tokens(market: dict[str, Any]) -> tuple[str, str]:
    """Polymarket exposes the two outcomes in market['tokens'] as a list of
    dicts with `outcome` and `token_id`. For BTC Up/Down markets the outcomes
    are labeled `Up` and `Down`. (For other Polymarket markets they'd be
    `Yes`/`No` — keep the lookup label-agnostic so this helper is reusable.)
    """
    tokens = market.get("tokens") or []
    up_id: str | None = None
    down_id: str | None = None
    for t in tokens:
        outcome = (t.get("outcome") or "").strip().lower()
        token_id = str(t.get("token_id") or "")
        if outcome in ("up", "yes"):
            up_id = token_id
        elif outcome in ("down", "no"):
            down_id = token_id
    if not up_id or not down_id:
        raise RuntimeError(
            f"Could not extract Up/Down token IDs from market {market.get('condition_id')!r}. "
            f"Got tokens={tokens!r}"
        )
    return up_id, down_id


def _parse_iso8601(iso: str) -> float:
    """Lenient ISO-8601 parse that tolerates trailing Z."""
    from datetime import datetime, timezone

    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
