"""CLI for the queue-conservative two-sided quoting simulator.

    python -m src.simulate recordings/<recording>
    python -m src.simulate recordings/<recording> --grid
    python -m src.simulate recordings/<recording> --json

Nothing is submitted. No credentials are read. The simulator only replays a
recording that already exists on disk.

Queue position cannot be recovered from public data, so `--grid` sweeps latency,
queue depth, and the complementary-fill assumption and reports the range. Treat
the worst corner of that range as the result, not the best.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.market_spec import build_market_spec
from src.orderbook import load_recording
from src.simulator import Assumptions, SimResult, simulate, simulate_grid, taker_fee_rate


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Simulate two-sided maker quoting over a recorded market."
    )
    parser.add_argument("recording", type=Path, help="Recording directory or events.jsonl")
    parser.add_argument("--grid", action="store_true",
                        help="Sweep latency, queue depth and the complementary-fill assumption")
    parser.add_argument("--json", action="store_true", help="Print results as JSON")
    parser.add_argument("--size", type=float, default=100.0, help="Order size in shares")
    parser.add_argument("--min-edge", type=float, default=0.01,
                        help="Required 1.00 minus pair cost before quoting")
    parser.add_argument("--latency-ms", type=int, default=1400,
                        help="Place and cancel latency for a single run")
    parser.add_argument("--queue-multiple", type=float, default=1.0,
                        help="Multiple of resting size assumed ahead of us")
    parser.add_argument("--quote-mode", choices=("join", "improve"), default="join")
    parser.add_argument("--max-unpaired", type=float, default=200.0,
                        help="Unpaired share cap before a breach is recorded")
    parser.add_argument("--max-unpaired-hold-ms", type=int, default=60_000)
    parser.add_argument("--complementary-fills", action="store_true",
                        help="Count complementary mints as consuming our queue")
    args = parser.parse_args()

    rows, metadata = load_recording(args.recording)
    market = metadata.get("market") or {}
    if not market:
        print("Recording has no market metadata — cannot simulate.")
        return 1
    spec = build_market_spec(_spec_payload(market))

    base = Assumptions(
        place_latency_ms=args.latency_ms,
        cancel_latency_ms=args.latency_ms,
        queue_ahead_multiple=args.queue_multiple,
        order_size_shares=args.size,
        min_edge=args.min_edge,
        max_unpaired_shares=args.max_unpaired,
        max_unpaired_hold_ms=args.max_unpaired_hold_ms,
        quote_mode=args.quote_mode,
        complementary_fills=args.complementary_fills,
    )

    results = simulate_grid(rows, spec, base) if args.grid else [simulate(rows, spec, base)]

    if args.json:
        print(json.dumps([result.to_dict() for result in results], indent=2, sort_keys=True))
    else:
        print(_render(results, spec, grid=args.grid))
    return 0


def _spec_payload(market: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a CLOB-shaped payload from recorded metadata."""
    raw = market.get("raw")
    if isinstance(raw, dict) and raw.get("condition_id"):
        return raw
    return {
        "condition_id": market.get("condition_id", ""),
        "question": market.get("question", ""),
        "market_slug": market.get("market_slug", ""),
        "minimum_tick_size": market.get("minimum_tick_size", 0.01),
        "minimum_order_size": market.get("minimum_order_size", 0),
        "maker_base_fee": market.get("maker_base_fee", 0),
        "taker_base_fee": market.get("taker_base_fee", 0),
        "tags": market.get("tags", []),
        "rewards": market.get("rewards", {}),
        "tokens": [
            {"token_id": outcome.get("token_id"), "outcome": outcome.get("label"),
             "price": outcome.get("price"), "winner": outcome.get("winner")}
            for outcome in market.get("outcomes", [])
        ],
    }


def _render(results: list[SimResult], spec: Any, *, grid: bool) -> str:
    lines: list[str] = []
    lines.append("=" * 96)
    lines.append(f"Market      {spec.market_slug}")
    lines.append(
        f"Config      tick={spec.minimum_tick_size} min_order_size={spec.minimum_order_size} "
        f"taker_fee_rate={taker_fee_rate(spec):g} (close-out only; makers pay none)"
    )
    lines.append(
        f"Rewards     daily_rate={spec.rewards.daily_rate_total} "
        f"min_size={spec.rewards.min_size} max_spread={spec.rewards.max_spread} "
        f"— eligibility time only, never credited to PnL"
    )
    lines.append("=" * 96)

    header = (
        f"{'assumptions':<42} {'fills':>6} {'pairs':>8} {'net USD':>10} "
        f"{'ROC':>9} {'unpaired':>9} {'breach':>7}"
    )
    lines.append(header)
    lines.append("-" * 96)
    for result in results:
        data = result.to_dict()
        roc = data["return_on_capital"]
        lines.append(
            f"{data['assumptions']:<42} {data['fills']:>6} {data['pairs_merged']:>8.1f} "
            f"{data['net_cash']:>10.4f} "
            f"{('n/a' if roc is None else f'{roc:>8.4%}'):>9} "
            f"{data['unpaired']['peak_shares']:>9.1f} {data['breaches']:>7}"
        )
    lines.append("-" * 96)

    first = results[0].to_dict()
    lines.append(
        f"Duration    {first['duration_s']}s   "
        f"quoting {first['quoting_time_s']}s, two-sided {first['two_sided_time_s']}s, "
        f"reward-eligible {first['reward_eligible_time_s']}s"
    )
    lines.append(
        f"Orders      {first['orders_placed']} placed, {first['orders_cancelled']} cancelled, "
        f"{first['fills_during_cancel_race']} fills landed during a cancel race"
    )
    lines.append(
        f"Blocked     {first['unquotable_blocks']} decisions skipped on book quality, "
        f"{first['blocked_by_edge']} on insufficient edge"
    )
    cost = first["pair_cost"]
    lines.append(
        f"Pair cost   median={cost['median']} min={cost['min']} max={cost['max']} "
        f"(n={cost['samples']}) — best available edge {cost['best_edge']}"
    )

    share = first["reward_share"]
    if share["samples"]:
        lines.append(
            f"Rewards     qualifying depth within max_spread median "
            f"{share['median_qualifying_depth']} shares; our size is "
            f"{share['our_share_upper_bound']:.4%} of it (upper bound on pool share, "
            f"before the scoring function — still not credited)"
        )

    queue = first["queue"]
    lines.append(
        f"Queue       median {queue['median_ahead_shares']} shares ahead of us at placement; "
        f"{queue['consuming_volume_shares']} shares of consuming flow reached our price"
    )
    if (
        queue["median_ahead_shares"]
        and queue["consuming_volume_shares"] < queue["median_ahead_shares"]
    ):
        lines.append(
            "            Flow never exceeded the queue. A queue-joining maker does not fill here."
        )

    nets = [result.net_cash for result in results]
    if grid:
        lines.append("")
        lines.append(
            f"Range       net USD {min(nets):.4f} to {max(nets):.4f} across "
            f"{len(results)} assumption points"
        )
        lines.append("            Take the worst corner as the result. Queue position is unknowable.")
    if all(result.pairs_merged == 0 for result in results):
        lines.append("")
        lines.append("NO PAIRS MERGED — no assumption point produced a matched pair.")
        lines.append("Nothing here supports an edge estimate. See the counts above for why.")
    lines.append("")
    lines.append("Rewards and maker rebates are NOT credited. They depend on other makers'")
    lines.append("behaviour, which public data does not contain.")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
