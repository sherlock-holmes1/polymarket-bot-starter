"""Queue-conservative simulator for two-sided maker quoting.

Replays a recording and maintains simulated resting orders alongside the
reconstructed book. Nothing is submitted anywhere; no credentials are used.

The strategy under test: post bids on both complementary outcomes. A matched
YES/NO pair merges into USD 1.00 without waiting for resolution, so the edge is
`1.00 - (price_up + price_down)` less costs. The interval between one leg filling
and the other is directional inventory risk, and is measured, not assumed away.

Conservative means every unresolvable question is answered against the strategy:

  * **Queue.** All size resting at my price when I arrive is ahead of me. Cancels
    at my level are assumed to be behind me, so they never move me up.
  * **Latency.** An order is not live until `place_latency_ms` after the decision,
    and a cancel does not take effect until `cancel_latency_ms` after it — so a
    fill can still land during the cancel race.
  * **Fills.** Counted only from recorded `last_trade_price` events whose taker
    side consumed my side of the book. A price merely touching my quote is not a
    fill.
  * **Close-out.** Unpaired inventory is sold into the bid as a taker, paying the
    taker fee.
  * **Rewards.** Liquidity rewards and maker rebates depend on other makers'
    behaviour, which public data does not contain. Eligible quoting time is
    reported; no reward income is ever credited to PnL.

Because queue position is unknowable from public data, a single run is not a
result. `simulate_grid()` sweeps the assumptions and reports the range.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from src.market_spec import MarketSpec
from src.orderbook import DEFAULT_MAX_STALE_MS, OrderBook, Tick, Trade, walk

# Taker fee rates by market category, from docs/polymarket/trading/fees.md.
# fee = shares x rate x p x (1 - p). Makers are never charged.
TAKER_FEE_RATES = {
    "crypto": 0.07, "sports": 0.03, "finance": 0.04, "politics": 0.04,
    "economics": 0.05, "culture": 0.05, "weather": 0.05, "tech": 0.04,
    "mentions": 0.04, "geopolitics": 0.0, "default": 0.05,
}


@dataclass(frozen=True)
class Assumptions:
    """One point in the assumption space. Every field worsens results as it rises."""

    place_latency_ms: int = 1400
    cancel_latency_ms: int = 1400
    queue_ahead_multiple: float = 1.0
    order_size_shares: float = 100.0
    min_edge: float = 0.01
    max_unpaired_shares: float = 200.0
    max_unpaired_hold_ms: int = 60_000
    quote_mode: str = "join"
    complementary_fills: bool = False
    max_stale_ms: int = DEFAULT_MAX_STALE_MS

    def label(self) -> str:
        return (
            f"lat={self.place_latency_ms}ms q={self.queue_ahead_multiple:g}x "
            f"edge={self.min_edge:g} {self.quote_mode}"
            f"{' +comp' if self.complementary_fills else ''}"
        )


@dataclass
class RestingOrder:
    """A simulated post-only bid."""

    asset_id: str
    price: float
    size: float
    placed_at_ms: int
    live_at_ms: int
    queue_ahead: float
    consumed: float = 0.0
    filled: float = 0.0
    cancel_requested_at_ms: int | None = None
    cancel_effective_at_ms: int | None = None

    def is_live(self, now_ms: int) -> bool:
        if now_ms < self.live_at_ms:
            return False
        if self.cancel_effective_at_ms is not None and now_ms >= self.cancel_effective_at_ms:
            return False
        return self.filled < self.size

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)

    def absorb(self, volume: float) -> float:
        """Apply consuming volume. Returns newly filled shares."""
        self.consumed += volume
        reachable = max(0.0, self.consumed - self.queue_ahead)
        newly = min(reachable, self.size) - self.filled
        if newly <= 0:
            return 0.0
        self.filled += newly
        return newly


@dataclass
class Fill:
    at_ms: int
    asset_id: str
    price: float
    shares: float
    during_cancel_race: bool


@dataclass
class Breach:
    at_ms: int
    kind: str
    detail: str


@dataclass
class SimResult:
    """Cash accounting for one assumption point. Rewards are never credited."""

    assumptions: Assumptions
    market_slug: str = ""
    duration_s: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    breaches: list[Breach] = field(default_factory=list)
    shares_bought: dict[str, float] = field(default_factory=dict)
    cash_spent: float = 0.0
    pairs_merged: float = 0.0
    merge_proceeds: float = 0.0
    closeout_proceeds: float = 0.0
    closeout_fees: float = 0.0
    closeout_shares: dict[str, float] = field(default_factory=dict)
    unpaired_intervals_ms: list[int] = field(default_factory=list)
    peak_unpaired_shares: float = 0.0
    capital_locked_peak: float = 0.0
    pair_cost_samples: list[float] = field(default_factory=list)
    queue_ahead_samples: list[float] = field(default_factory=list)
    consuming_volume: float = 0.0
    reward_depth_samples: list[float] = field(default_factory=list)
    blocked_by_edge: int = 0
    quoting_time_ms: int = 0
    two_sided_time_ms: int = 0
    reward_eligible_time_ms: int = 0
    orders_placed: int = 0
    orders_cancelled: int = 0
    fills_during_cancel_race: int = 0
    unquotable_blocks: int = 0

    @property
    def net_cash(self) -> float:
        return self.merge_proceeds + self.closeout_proceeds - self.cash_spent - self.closeout_fees

    @property
    def return_on_capital(self) -> float | None:
        if self.capital_locked_peak <= 0:
            return None
        return self.net_cash / self.capital_locked_peak

    def to_dict(self) -> dict[str, Any]:
        intervals = sorted(self.unpaired_intervals_ms)
        return {
            "assumptions": self.assumptions.label(),
            "duration_s": round(self.duration_s, 1),
            "orders_placed": self.orders_placed,
            "orders_cancelled": self.orders_cancelled,
            "fills": len(self.fills),
            "fills_during_cancel_race": self.fills_during_cancel_race,
            "shares_bought": {k: round(v, 2) for k, v in sorted(self.shares_bought.items())},
            "pairs_merged": round(self.pairs_merged, 2),
            "cash_spent": round(self.cash_spent, 4),
            "merge_proceeds": round(self.merge_proceeds, 4),
            "closeout_proceeds": round(self.closeout_proceeds, 4),
            "closeout_fees": round(self.closeout_fees, 4),
            "net_cash": round(self.net_cash, 4),
            "capital_locked_peak": round(self.capital_locked_peak, 4),
            "return_on_capital": (
                None if self.return_on_capital is None else round(self.return_on_capital, 6)
            ),
            "unpaired": {
                "peak_shares": round(self.peak_unpaired_shares, 2),
                "intervals": len(intervals),
                "median_ms": intervals[len(intervals) // 2] if intervals else None,
                "max_ms": intervals[-1] if intervals else None,
            },
            "breaches": len(self.breaches),
            "quoting_time_s": round(self.quoting_time_ms / 1000, 1),
            "two_sided_time_s": round(self.two_sided_time_ms / 1000, 1),
            "reward_eligible_time_s": round(self.reward_eligible_time_ms / 1000, 1),
            "unquotable_blocks": self.unquotable_blocks,
            "blocked_by_edge": self.blocked_by_edge,
            "pair_cost": _summarize_pair_cost(self.pair_cost_samples),
            "reward_share": _summarize_reward_share(
                self.reward_depth_samples, self.assumptions.order_size_shares
            ),
            "queue": {
                "median_ahead_shares": (
                    round(sorted(self.queue_ahead_samples)[len(self.queue_ahead_samples) // 2], 2)
                    if self.queue_ahead_samples else None
                ),
                "consuming_volume_shares": round(self.consuming_volume, 2),
            },
            "rewards_credited": False,
        }


def _summarize_pair_cost(samples: list[float]) -> dict[str, float | int | None]:
    """Cost of buying both outcomes at our target prices — the whole edge."""
    if not samples:
        return {"samples": 0, "min": None, "median": None, "max": None, "best_edge": None}
    ordered = sorted(samples)
    median = ordered[len(ordered) // 2]
    return {
        "samples": len(samples),
        "min": round(ordered[0], 4),
        "median": round(median, 4),
        "max": round(ordered[-1], 4),
        "best_edge": round(1.0 - ordered[0], 4),
    }


def _summarize_reward_share(depths: list[float], our_size: float) -> dict[str, Any]:
    """Our resting size against the qualifying depth competing for the same pool."""
    if not depths:
        return {"samples": 0, "median_qualifying_depth": None, "our_share_upper_bound": None}
    ordered = sorted(depths)
    median = ordered[len(ordered) // 2]
    # Two sides are quoted, so our qualifying size is counted on both.
    share = (2 * our_size) / median if median > 0 else None
    return {
        "samples": len(depths),
        "median_qualifying_depth": round(median, 1),
        "our_share_upper_bound": None if share is None else round(share, 6),
    }


def taker_fee_rate(spec: MarketSpec) -> float:
    """Category taker rate for close-out costing. Makers are never charged."""
    for tag in spec.tags:
        rate = TAKER_FEE_RATES.get(tag.strip().lower())
        if rate is not None:
            return rate
    return TAKER_FEE_RATES["default"]


def taker_fee(shares: float, price: float, rate: float) -> float:
    """fee = C x rate x p x (1 - p) — docs/polymarket/trading/fees.md."""
    return shares * rate * price * (1.0 - price)


class TwoSidedQuoter:
    """Posts bids on both outcomes whenever the pair can be bought under USD 1.00."""

    def __init__(self, spec: MarketSpec, assumptions: Assumptions) -> None:
        if len(spec.outcomes) != 2:
            raise ValueError("two-sided quoting requires exactly two outcomes")
        self.spec = spec
        self.a = assumptions
        self.fee_rate = taker_fee_rate(spec)
        self.assets = [outcome.token_id for outcome in spec.outcomes]
        self.complement = {self.assets[0]: self.assets[1], self.assets[1]: self.assets[0]}
        self.result = SimResult(assumptions=assumptions, market_slug=spec.market_slug)
        self._orders: dict[str, RestingOrder] = {}
        self._inventory: dict[str, float] = {asset: 0.0 for asset in self.assets}
        self._unpaired_since_ms: int | None = None
        self._last_ms: int | None = None

    # -- main loop ---------------------------------------------------------

    def run(self, ticks: Iterable[Tick]) -> SimResult:
        first_ms: int | None = None
        last_tick: Tick | None = None
        for tick in ticks:
            if tick.at_ms is None:
                continue
            if first_ms is None:
                first_ms = tick.at_ms
            self._accrue_time(tick)
            self._apply_trades(tick)
            self._manage_quotes(tick)
            self._enforce_inventory(tick)
            self._last_ms = tick.at_ms
            last_tick = tick
        if first_ms is not None and self._last_ms is not None:
            self.result.duration_s = (self._last_ms - first_ms) / 1000
        if last_tick is not None:
            self._close_out(last_tick)
        return self.result

    # -- time accounting ---------------------------------------------------

    def _accrue_time(self, tick: Tick) -> None:
        if self._last_ms is None or tick.at_ms is None:
            return
        elapsed = tick.at_ms - self._last_ms
        if elapsed <= 0:
            return
        live = [o for o in self._orders.values() if o.is_live(self._last_ms)]
        if live:
            self.result.quoting_time_ms += elapsed
        if len({o.asset_id for o in live}) == 2:
            self.result.two_sided_time_ms += elapsed
            if all(self._reward_eligible(o, tick) for o in live):
                self.result.reward_eligible_time_ms += elapsed
                self.result.reward_depth_samples.append(
                    sum(self._qualifying_depth(o, tick) for o in live)
                )
        if self._unpaired_shares() > self.a.max_unpaired_shares:
            self.result.breaches.append(Breach(
                at_ms=tick.at_ms, kind="unpaired_cap",
                detail=f"{self._unpaired_shares():.1f} shares above cap {self.a.max_unpaired_shares:.1f}",
            ))

    def _reward_eligible(self, order: RestingOrder, tick: Tick) -> bool:
        """Within the market's reward max_spread of midpoint, and at least min_size."""
        rewards = self.spec.rewards
        if rewards.min_size and order.size < rewards.min_size:
            return False
        book = tick.books.get(order.asset_id)
        if book is None or book.best_bid is None or book.best_ask is None:
            return False
        midpoint = (book.best_bid + book.best_ask) / 2
        if rewards.max_spread is None:
            return False
        return abs(midpoint - order.price) * 100 <= rewards.max_spread

    def _qualifying_depth(self, order: RestingOrder, tick: Tick) -> float:
        """Resting size on our side within the market's reward max_spread.

        Public and observable. Our own share of the reward pool is roughly our
        size over this, before the scoring function — enough for an order of
        magnitude, not enough to credit as income.
        """
        book = tick.books.get(order.asset_id)
        max_spread = self.spec.rewards.max_spread
        if book is None or max_spread is None or book.best_bid is None or book.best_ask is None:
            return 0.0
        midpoint = (book.best_bid + book.best_ask) / 2
        return sum(
            size for price, size in book.bids.items()
            if abs(midpoint - float(price)) * 100 <= max_spread
        )

    # -- fills -------------------------------------------------------------

    def _apply_trades(self, tick: Tick) -> None:
        for trade in tick.trades:
            for order in list(self._orders.values()):
                volume = self._consuming_volume(order, trade)
                if volume <= 0 or not order.is_live(tick.at_ms or 0):
                    continue
                self.result.consuming_volume += volume
                newly = order.absorb(volume)
                if newly <= 0:
                    continue
                self._record_fill(order, newly, tick)

    def _consuming_volume(self, order: RestingOrder, trade: Trade) -> float:
        """Volume from `trade` that consumed this resting bid's queue.

        A taker SELL at my price on my asset hits my side of the book. The venue
        can also mint a complementary pair — a taker BUY of the other outcome at
        (1 - my price) — which may consume my bid too. That path cannot be
        distinguished from public data, so it is off by default and becomes an
        explicit assumption axis rather than a hidden choice.
        """
        if trade.asset_id == order.asset_id and trade.side == "SELL":
            if abs(trade.price - order.price) < 1e-9:
                return trade.size
        if self.a.complementary_fills and trade.asset_id == self.complement[order.asset_id]:
            if trade.side == "BUY" and abs((1.0 - trade.price) - order.price) < 1e-9:
                return trade.size
        return 0.0

    def _record_fill(self, order: RestingOrder, shares: float, tick: Tick) -> None:
        at_ms = tick.at_ms or 0
        racing = order.cancel_requested_at_ms is not None
        self.result.fills.append(Fill(
            at_ms=at_ms, asset_id=order.asset_id, price=order.price,
            shares=shares, during_cancel_race=racing,
        ))
        if racing:
            self.result.fills_during_cancel_race += 1
        self.result.cash_spent += shares * order.price
        self.result.shares_bought[order.asset_id] = (
            self.result.shares_bought.get(order.asset_id, 0.0) + shares
        )
        self._inventory[order.asset_id] += shares
        self.result.capital_locked_peak = max(
            self.result.capital_locked_peak, self.result.cash_spent
        )
        self._merge_pairs(at_ms)

    def _merge_pairs(self, at_ms: int) -> None:
        """Merge matched shares into USD 1.00 and close the unpaired interval."""
        pairs = min(self._inventory[asset] for asset in self.assets)
        if pairs > 0:
            for asset in self.assets:
                self._inventory[asset] -= pairs
            self.result.pairs_merged += pairs
            self.result.merge_proceeds += pairs
            if self._unpaired_since_ms is not None and self._unpaired_shares() <= 0:
                self.result.unpaired_intervals_ms.append(at_ms - self._unpaired_since_ms)
                self._unpaired_since_ms = None
        unpaired = self._unpaired_shares()
        self.result.peak_unpaired_shares = max(self.result.peak_unpaired_shares, unpaired)
        if unpaired > 0 and self._unpaired_since_ms is None:
            self._unpaired_since_ms = at_ms

    def _unpaired_shares(self) -> float:
        return sum(self._inventory.values())

    # -- quoting -----------------------------------------------------------

    def _manage_quotes(self, tick: Tick) -> None:
        at_ms = tick.at_ms or 0
        for asset_id, order in list(self._orders.items()):
            if order.filled >= order.size or not order.is_live(at_ms):
                if order.cancel_effective_at_ms is not None and at_ms >= order.cancel_effective_at_ms:
                    del self._orders[asset_id]
                elif order.filled >= order.size:
                    del self._orders[asset_id]

        targets = self._target_prices(tick)
        if targets is None:
            for order in self._orders.values():
                self._request_cancel(order, at_ms)
            return

        for asset_id, price in targets.items():
            order = self._orders.get(asset_id)
            if order is not None:
                if abs(order.price - price) > 1e-9 and order.cancel_requested_at_ms is None:
                    self._request_cancel(order, at_ms)
                continue
            book = tick.books[asset_id]
            self._place(asset_id, price, book, at_ms)

    def _target_prices(self, tick: Tick) -> dict[str, float] | None:
        """Bid prices for both outcomes, or None when quoting is not justified."""
        if tick.gap_open:
            self.result.unquotable_blocks += 1
            return None
        prices: dict[str, float] = {}
        for asset_id in self.assets:
            if not tick.quotable(asset_id, self.a.max_stale_ms):
                self.result.unquotable_blocks += 1
                return None
            book = tick.books[asset_id]
            best_bid, best_ask = book.best_bid, book.best_ask
            if best_bid is None or best_ask is None:
                return None
            tick_size = self.spec.minimum_tick_size or 0.01
            price = best_bid if self.a.quote_mode == "join" else round(best_bid + tick_size, 10)
            if price >= best_ask:
                return None
            prices[asset_id] = price
        pair_cost = sum(prices.values())
        self.result.pair_cost_samples.append(pair_cost)
        if pair_cost > 1.0 - self.a.min_edge:
            self.result.blocked_by_edge += 1
            return None
        return prices

    def _place(self, asset_id: str, price: float, book: OrderBook, at_ms: int) -> None:
        level = book.bids.get(_level_key(book.bids, price), 0.0)
        self._orders[asset_id] = RestingOrder(
            asset_id=asset_id,
            price=price,
            size=self.a.order_size_shares,
            placed_at_ms=at_ms,
            live_at_ms=at_ms + self.a.place_latency_ms,
            queue_ahead=level * self.a.queue_ahead_multiple,
        )
        self.result.queue_ahead_samples.append(level * self.a.queue_ahead_multiple)
        self.result.orders_placed += 1

    def _request_cancel(self, order: RestingOrder, at_ms: int) -> None:
        if order.cancel_requested_at_ms is not None:
            return
        order.cancel_requested_at_ms = at_ms
        order.cancel_effective_at_ms = at_ms + self.a.cancel_latency_ms
        self.result.orders_cancelled += 1

    # -- inventory ---------------------------------------------------------

    def _enforce_inventory(self, tick: Tick) -> None:
        at_ms = tick.at_ms or 0
        if self._unpaired_since_ms is None:
            return
        held_ms = at_ms - self._unpaired_since_ms
        if held_ms > self.a.max_unpaired_hold_ms:
            self.result.breaches.append(Breach(
                at_ms=at_ms, kind="unpaired_hold",
                detail=f"{held_ms}ms above max {self.a.max_unpaired_hold_ms}ms",
            ))
            self._unpaired_since_ms = at_ms  # one breach per window, not per event

    # -- close-out ---------------------------------------------------------

    def _close_out(self, tick: Tick) -> None:
        """Sell leftover inventory into the bid as a taker, paying the taker fee."""
        for asset_id, shares in self._inventory.items():
            if shares <= 0:
                continue
            book = tick.books.get(asset_id)
            bid = book.best_bid if book is not None else None
            price = bid if bid is not None else 0.0
            self.result.closeout_shares[asset_id] = shares
            self.result.closeout_proceeds += shares * price
            self.result.closeout_fees += taker_fee(shares, price, self.fee_rate)


def simulate(
    rows: Iterable[dict[str, Any]], spec: MarketSpec, assumptions: Assumptions
) -> SimResult:
    return TwoSidedQuoter(spec, assumptions).run(walk(rows))


def simulate_grid(
    rows: list[dict[str, Any]],
    spec: MarketSpec,
    base: Assumptions,
    *,
    latencies_ms: tuple[int, ...] = (700, 1400, 3000),
    queue_multiples: tuple[float, ...] = (1.0, 2.0),
    complementary: tuple[bool, ...] = (False, True),
) -> list[SimResult]:
    """Sweep the unresolvable assumptions. A single point is not a result."""
    results: list[SimResult] = []
    for latency in latencies_ms:
        for multiple in queue_multiples:
            for comp in complementary:
                point = replace(
                    base,
                    place_latency_ms=latency,
                    cancel_latency_ms=latency,
                    queue_ahead_multiple=multiple,
                    complementary_fills=comp,
                )
                results.append(simulate(rows, spec, point))
    return results


def _level_key(levels: dict[str, float], price: float) -> str:
    for key in levels:
        if abs(float(key) - price) < 1e-9:
            return key
    return ""
