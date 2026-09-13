"""CLI for deterministic replay of public market recordings.

    python -m src.replay recordings/<recording>            # human-readable report
    python -m src.replay recordings/<recording> --json     # machine-readable
    python -m src.replay recordings/<recording> --verify   # prove determinism

`--verify` re-reads the file and replays it a second time, then compares the
state digests. Identical digests mean the same input produced the same sequence
of book states.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.orderbook import (
    DEFAULT_DIVERGENCE_TOLERANCE,
    DEFAULT_MAX_STALE_MS,
    ReplayReport,
    labels_from_metadata,
    load_recording,
    replay,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a recorded Polymarket market-data stream.")
    parser.add_argument("recording", type=Path, help="Recording directory or events.jsonl path")
    parser.add_argument("--max-stale-ms", type=int, default=DEFAULT_MAX_STALE_MS,
                        help="A book older than this is not quotable (default: %(default)s)")
    parser.add_argument("--divergence-tolerance", type=int, default=DEFAULT_DIVERGENCE_TOLERANCE,
                        help="Consecutive disagreements with the venue's top of book before a "
                             "book is rejected as diverged (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON")
    parser.add_argument("--verify", action="store_true",
                        help="Replay twice and confirm the state digests match")
    args = parser.parse_args()

    rows, metadata = load_recording(args.recording)
    labels = labels_from_metadata(metadata)
    report = replay(rows, max_stale_ms=args.max_stale_ms,
                    divergence_tolerance=args.divergence_tolerance, labels=labels)

    verified: bool | None = None
    if args.verify:
        rows_again, _ = load_recording(args.recording)
        second = replay(rows_again, max_stale_ms=args.max_stale_ms,
                        divergence_tolerance=args.divergence_tolerance, labels=labels)
        verified = second.digest == report.digest

    if args.json:
        payload: dict[str, Any] = report.to_dict()
        if verified is not None:
            payload["determinism_verified"] = verified
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_render(report, metadata, verified))

    if verified is False:
        print("DETERMINISM CHECK FAILED — two replays of the same file disagreed", file=sys.stderr)
        return 1
    return 0


def _render(report: ReplayReport, metadata: dict[str, Any], verified: bool | None) -> str:
    market = metadata.get("market") or {}
    data = report.to_dict()
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"Market      {market.get('market_slug') or '(unknown)'}")
    if market.get("condition_id"):
        lines.append(f"Condition   {market['condition_id']}")
    lines.append(
        f"Config      tick={market.get('minimum_tick_size')} "
        f"min_order_size={market.get('minimum_order_size')} "
        f"maker_fee_bps={market.get('maker_base_fee')} taker_fee_bps={market.get('taker_base_fee')}"
    )
    rewards = market.get("rewards") or {}
    lines.append(
        f"Rewards     daily_rate={_reward_rate(rewards)} min_size={rewards.get('min_size')} "
        f"max_spread={rewards.get('max_spread')}"
    )
    lines.append("=" * 72)
    lines.append(f"Duration    {data['duration_s']}s  ({data['started_at']} → {data['ended_at']})")
    lines.append(f"Events      {data['events']}  {_compact(data['by_kind'])}")
    lines.append(f"Event types {_compact(data['by_event_type']) or '(none)'}")
    lines.append(
        f"Broadcasts  {data['broadcast_events']} venue-wide events for other markets "
        f"(excluded from this market's stats)"
    )
    for resolution in data["resolutions"]:
        lines.append(f"Resolved    {resolution['slug']} → {resolution['winning_outcome']} at {resolution['at']}")
    lines.append(f"Connection  {_compact(data['connection_states']) or '(none)'}")
    lines.append(
        f"Gaps        {data['gaps']['count']} covering {data['gaps']['total_s']}s"
    )
    for gap in data["gaps"]["detail"]:
        state = "OPEN AT END" if gap["open_at_end_of_recording"] else f"{gap['duration_s']}s"
        lines.append(f"              {gap['opened_at']} → {gap['closed_at'] or '—'}  {state}  ({gap['reason']})")
    latency = data["timestamps"]["exchange_to_receipt_ms"]
    lines.append(
        f"Latency     exchange→receipt median={latency['median']}ms max={latency['max']}ms "
        f"n={latency['samples']} (deltas and trades only; snapshot timestamps are last-change times)"
    )
    lines.append("-" * 72)
    for asset_id, asset in data["quoting"]["assets"].items():
        label = asset["label"] or "?"
        lines.append(f"{label:<6} {asset_id[:20]}…")
        counts = asset["counts"]
        lines.append(
            f"       events   snapshots={counts['snapshots']} price_changes={counts['price_changes']} "
            f"trades={counts['trades']} orphans={counts['orphan_price_changes']} "
            f"crossed={counts['crossed_observations']} "
            f"top_mismatches={counts['top_of_book_mismatches']}"
        )
        lines.append(
            f"       bba      {counts['best_bid_ask_disagreements']}/{counts['best_bid_ask_checks']} "
            f"best_bid_ask reads disagreed (observation only — does not reject)"
        )
        fraction = asset["quotable_fraction"]
        lines.append(
            f"       quotable {'n/a' if fraction is None else f'{fraction:.1%}'}  "
            f"{_compact({k: f'{v}s' for k, v in asset['state_seconds'].items()})}"
        )
        spread, bid_depth, ask_depth = asset["spread"], asset["depth_at_best_bid"], asset["depth_at_best_ask"]
        lines.append(
            f"       spread   median={spread['median']} min={spread['min']} max={spread['max']} "
            f"n={spread['samples']}"
        )
        lines.append(
            f"       depth    best_bid median={bid_depth['median']} best_ask median={ask_depth['median']} "
            f"book_bids median={asset['total_depth_bids']['median']} "
            f"book_asks median={asset['total_depth_asks']['median']}"
        )
    lines.append("-" * 72)
    lines.append(f"Digest      {data['digest']}")
    if verified is not None:
        lines.append(f"Determinism {'PASS — two replays agree' if verified else 'FAIL — replays disagree'}")
    lines.append("Fills       not inferred — trades counted only from last_trade_price events")
    if data["warnings"]:
        lines.append("Warnings")
        for warning in data["warnings"]:
            lines.append(f"  ! {warning}")
    else:
        lines.append("Warnings    none")
    return "\n".join(lines)


def _reward_rate(rewards: dict[str, Any]) -> float:
    return sum(float(rate.get("rewards_daily_rate") or 0.0) for rate in rewards.get("rates") or [])


def _compact(mapping: dict[str, Any]) -> str:
    return " ".join(f"{key}={value}" for key, value in mapping.items())


if __name__ == "__main__":
    raise SystemExit(main())
