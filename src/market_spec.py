"""Explicit market selection and full public spec capture.

Milestone step 1: pick one market on purpose and record everything the venue
publishes about how it trades — outcome token IDs with their *verbatim* labels,
tick size, minimum order size, the fee schedule, and the liquidity-reward
configuration — each stamped with the moment it was observed.

Nothing here needs credentials. All three endpoints used are public:
  * CLOB  GET /markets/{condition_id}   (via py-clob-client-v2 `get_market`)
  * CLOB  GET /sampling-markets         (reward-enabled markets only)
  * Gamma GET /markets?slug=...         (slug -> condition_id resolution)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests
from py_clob_client_v2 import ClobClient

from src.utils import get_logger, retry_with_backoff

logger = get_logger(__name__)

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
BTC_UPDOWN_15M_SLUG_PREFIX = "btc-updown-15m"
BTC_UPDOWN_15M_WINDOW_S = 900


@dataclass(frozen=True)
class Outcome:
    """One tradeable outcome token. `label` is kept exactly as the venue spells it."""

    token_id: str
    label: str
    price: float | None
    winner: bool | None


@dataclass(frozen=True)
class RewardConfig:
    """Liquidity-reward configuration as published on the market."""

    min_size: float | None
    max_spread: float | None
    rates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def daily_rate_total(self) -> float:
        return sum(float(rate.get("rewards_daily_rate") or 0.0) for rate in self.rates)

    @property
    def is_reward_eligible(self) -> bool:
        return self.daily_rate_total > 0.0


@dataclass(frozen=True)
class MarketSpec:
    """Everything the public API says about how one market trades, at one instant."""

    observed_at: str
    observed_unix_ms: int
    condition_id: str
    question_id: str
    question: str
    market_slug: str
    description: str
    end_date_iso: str
    active: bool
    closed: bool
    archived: bool
    accepting_orders: bool
    accepting_order_timestamp: str
    enable_order_book: bool
    minimum_tick_size: float
    minimum_order_size: float
    maker_base_fee: int
    taker_base_fee: int
    neg_risk: bool
    is_50_50_outcome: bool
    tags: list[str]
    rewards: RewardConfig
    outcomes: list[Outcome]
    raw: dict[str, Any]

    @property
    def token_ids(self) -> list[str]:
        return [outcome.token_id for outcome in self.outcomes]

    @property
    def labels_by_token_id(self) -> dict[str, str]:
        return {outcome.token_id: outcome.label for outcome in self.outcomes}

    def describe(self) -> str:
        labels = "/".join(outcome.label for outcome in self.outcomes)
        return (
            f"{self.market_slug} [{labels}] tick={self.minimum_tick_size} "
            f"min_size={self.minimum_order_size} rewards_daily={self.rewards.daily_rate_total} "
            f"reward_min_size={self.rewards.min_size} reward_max_spread={self.rewards.max_spread}"
        )


def select_market(
    client: ClobClient,
    *,
    condition_id: str | None = None,
    slug: str | None = None,
) -> MarketSpec:
    """Resolve one explicitly chosen market. Exactly one selector is required."""
    if bool(condition_id) == bool(slug):
        raise ValueError("pass exactly one of condition_id or slug")
    resolved = condition_id or resolve_condition_id(str(slug))
    return fetch_market_spec(client, resolved)


def fetch_market_spec(client: ClobClient, condition_id: str) -> MarketSpec:
    """Read the CLOB market object for `condition_id` and stamp the observation."""
    market = retry_with_backoff(lambda: client.get_market(condition_id), logger=logger)
    if not isinstance(market, dict) or not market.get("condition_id"):
        raise RuntimeError(f"CLOB returned no market for condition_id={condition_id!r}: {market!r}")
    return build_market_spec(market)


def build_market_spec(market: dict[str, Any]) -> MarketSpec:
    """Convert a raw CLOB market payload into a timestamped `MarketSpec`."""
    now = datetime.now(timezone.utc)
    rewards_raw = market.get("rewards") or {}
    rewards = RewardConfig(
        min_size=_optional_float(rewards_raw.get("min_size")),
        max_spread=_optional_float(rewards_raw.get("max_spread")),
        rates=list(rewards_raw.get("rates") or []),
    )
    outcomes = [
        Outcome(
            token_id=str(token.get("token_id") or ""),
            label=str(token.get("outcome") or ""),
            price=_optional_float(token.get("price")),
            winner=token.get("winner"),
        )
        for token in market.get("tokens") or []
    ]
    missing = [outcome for outcome in outcomes if not outcome.token_id or not outcome.label]
    if len(outcomes) < 2 or missing:
        raise RuntimeError(
            f"market {market.get('condition_id')!r} has unusable outcome tokens: {market.get('tokens')!r}"
        )
    return MarketSpec(
        observed_at=now.isoformat().replace("+00:00", "Z"),
        observed_unix_ms=int(now.timestamp() * 1000),
        condition_id=str(market["condition_id"]),
        question_id=str(market.get("question_id") or ""),
        question=str(market.get("question") or ""),
        market_slug=str(market.get("market_slug") or ""),
        description=str(market.get("description") or ""),
        end_date_iso=str(market.get("end_date_iso") or ""),
        active=bool(market.get("active")),
        closed=bool(market.get("closed")),
        archived=bool(market.get("archived")),
        accepting_orders=bool(market.get("accepting_orders")),
        accepting_order_timestamp=str(market.get("accepting_order_timestamp") or ""),
        enable_order_book=bool(market.get("enable_order_book")),
        minimum_tick_size=float(market.get("minimum_tick_size") or 0.0),
        minimum_order_size=float(market.get("minimum_order_size") or 0.0),
        maker_base_fee=int(market.get("maker_base_fee") or 0),
        taker_base_fee=int(market.get("taker_base_fee") or 0),
        neg_risk=bool(market.get("neg_risk")),
        is_50_50_outcome=bool(market.get("is_50_50_outcome")),
        tags=[str(tag) for tag in market.get("tags") or []],
        rewards=rewards,
        outcomes=outcomes,
        raw=market,
    )


def resolve_condition_id(slug: str) -> str:
    """Look up a market slug on the public Gamma API and return its condition ID."""

    def _fetch() -> list[dict[str, Any]]:
        response = requests.get(f"{GAMMA_BASE_URL}/markets", params={"slug": slug}, timeout=15)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else payload.get("data") or []

    markets = retry_with_backoff(_fetch, logger=logger)
    for market in markets:
        condition_id = market.get("conditionId") or market.get("condition_id")
        if condition_id:
            return str(condition_id)
    raise RuntimeError(f"No market found on Gamma for slug={slug!r}")


def current_btc_updown_15m_slug(now_unix: float | None = None) -> str:
    """Slug of the BTC Up/Down 15M window currently open.

    The series names each window by its start epoch, e.g.
    `btc-updown-15m-1789326900`. Deriving the slug beats scanning `get_markets()`
    pages — the series is not near the front of that listing.
    """
    now = int(now_unix if now_unix is not None else time.time())
    window_start = now - (now % BTC_UPDOWN_15M_WINDOW_S)
    return f"{BTC_UPDOWN_15M_SLUG_PREFIX}-{window_start}"


def select_active_btc_updown_15m(client: ClobClient) -> MarketSpec:
    """Resolve the BTC Up/Down 15M market for the window currently open.

    Around a window boundary the next market can lag by a few seconds, so the
    previous window is used as a fallback while it is still accepting orders.
    """
    now = time.time()
    errors: list[str] = []
    for offset in (0, -BTC_UPDOWN_15M_WINDOW_S):
        slug = current_btc_updown_15m_slug(now + offset)
        try:
            spec = select_market(client, slug=slug)
        except Exception as exc:
            errors.append(f"{slug}: {exc!r}")
            continue
        if spec.accepting_orders and not spec.closed:
            return spec
        errors.append(f"{slug}: accepting_orders={spec.accepting_orders} closed={spec.closed}")
    raise RuntimeError(
        "No BTC Up/Down 15M market is accepting orders right now. "
        f"Tried: {'; '.join(errors)}"
    )


def list_reward_eligible_markets(
    client: ClobClient,
    *,
    max_pages: int = 5,
    min_daily_rate: float = 0.0,
) -> list[MarketSpec]:
    """Return markets from `/sampling-markets` — the venue's reward-enabled set.

    Sorted by published daily reward rate, highest first. Used to choose a market
    on purpose rather than by accident; the collector never trades it.
    """
    specs: list[MarketSpec] = []
    cursor: str | None = None
    for _ in range(max_pages):
        page = retry_with_backoff(
            lambda: client.get_sampling_markets(next_cursor=cursor) if cursor else client.get_sampling_markets(),
            logger=logger,
        )
        for market in page.get("data") or []:
            if not market.get("active") or market.get("closed") or not market.get("accepting_orders"):
                continue
            try:
                spec = build_market_spec(market)
            except RuntimeError as exc:
                logger.warning(f"Skipping malformed sampling market: {exc}")
                continue
            if spec.rewards.daily_rate_total >= min_daily_rate:
                specs.append(spec)
        cursor = page.get("next_cursor")
        if not cursor or cursor in ("", "LTE="):
            break
    specs.sort(key=lambda spec: spec.rewards.daily_rate_total, reverse=True)
    return specs


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)
