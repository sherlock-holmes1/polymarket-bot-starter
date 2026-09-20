# Polymarket Bot Starter

This is the starter project for the **How to Build a Polymarket Trading Bot in 2026** tutorial published by Poly Research and Robotics (`polyresearchrobotics.com`).

Keep this folder as your working codebase for the whole tutorial. Each step adds a small amount of code; by Step 8 you'll have a runnable bot.

## What's in here

```
polymarket-bot-starter/
├── CLAUDE.md                    # Project-level context auto-loaded by Claude Code
├── .env.example                 # Copy to .env and fill in your credentials
├── requirements.txt             # Python dependencies (pip install -r requirements.txt)
├── main.py                      # Entry point — wires every module together
├── src/
│   ├── clob.py                  # Authenticated CLOB client init
│   ├── markets.py               # Active market lookup — refreshes the slug each cycle
│   ├── price_feed.py            # Polymarket RTDS Chainlink (primary) + Coinbase WS (fallback)
│   ├── market_channel.py        # CLOB market WS — auto-resubscribes when the cycle rolls
│   ├── market_spec.py           # Explicit market selection + full public spec
│   ├── collector.py             # Read-only recorder (milestone 1)
│   ├── recording.py             # Append-only JSONL recordings, three clocks
│   ├── orderbook.py             # Deterministic replay + quotability rules
│   ├── replay.py                # Replay CLI
│   ├── simulator.py             # Queue-conservative fill model + accounting
│   ├── simulate.py              # Simulator CLI
│   ├── signal_engine.py         # Signal generation (filled in during Step 4)
│   ├── orders.py                # Order placement (filled in during Step 5)
│   ├── risk.py                  # Risk manager (filled in during Step 6)
│   ├── scheduler.py             # Main trading loop (filled in during Step 7)
│   └── utils.py                 # Logger and retry helpers
├── tests/
│   └── test_smoke.py            # Import smoke test
└── docs/
    ├── gotchas.md               # Hard-won knowledge — read this before you start
    ├── price-feeds/
    │   ├── polymarket-rtds-reference.md
    │   └── coinbase-advanced-trade-ws.md
    └── polymarket/              # Mirror of docs.polymarket.com (Markdown)
        ├── _llms.txt            # Index of all 161 doc pages
        ├── _llms-full.txt       # Every doc concatenated into one file
        └── ...
```

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Polymarket API keys and Polygon RPC URL
python main.py
```

`main.py` does nothing useful out of the box — it prints a banner and exits. The tutorial walks you through filling in each module.

## Read-only market-data recorder

The first research milestone: a public market-data recorder and a deterministic
replay. No credentials, no signing, no order submission — read-only collection
stays permitted where trading is not.

```bash
# 1. Pick a market on purpose (reward-enabled markets, highest daily rate first)
python -m src.collector --list-rewarded

# 2. Record it
python -m src.collector --slug btc-updown-15m-1789326900 --duration 900
python -m src.collector --condition-id 0xa3b3… --output recordings
python -m src.collector --btc-updown-15m        # follow the rolling 15M window
python -m src.collector --btc-updown-5m         # follow the rolling 5M window

# 3. Replay it
python -m src.replay recordings/<recording> --verify
python -m src.replay recordings/<recording> --json
```

### What a recording contains

`metadata.json` holds the market's full public trading configuration as observed,
with the timestamp of the observation: outcome token IDs with their **verbatim**
labels (`Up`/`Down`, `Yes`/`No`), tick size, minimum order size, maker and taker
base fees, and the liquidity-reward configuration (`min_size`, `max_spread`,
daily rate).

`events.jsonl` holds every event in arrival order, payloads untouched, each row
stamped with three clocks:

| Field | Clock |
|---|---|
| `exchange_timestamp_ms` | the venue's own timestamp, when it supplies one |
| `received_unix_ms` / `received_at` | local wall clock at receipt |
| `received_monotonic_ns` | local monotonic clock, immune to NTP steps |

Alongside the market events the file records `market_spec` re-observations,
`connection` state changes, and `collector_gap` markers.

### Gaps

The market channel reconnects whenever the stream closes — not only when the
market rolls — and a close wakes the supervisor immediately rather than waiting
out the poll interval. A drop opens a `collector_gap`, which closes only once a
fresh book snapshot has arrived for **every** subscribed token.

### What replay asserts

Replay rebuilds one local book per outcome and decides, at every instant, whether
that book is fit to quote against. A book is not quotable while it is
un-snapshotted, inside a gap, one-sided, crossed, older than `--max-stale-ms`, or
**diverged from the venue's own top of book**. Time in each state is accounted
for, so a recording that looks busy but was unquotable for most of its length
cannot pass as good data.

Divergence is the check that catches a silently dropped delta — the failure a
determinism check cannot see, because a lossy recorder replays reproducibly.
Every `price_change` carries the venue's own best bid and ask for that update, so
the rebuilt book is compared against it in-band on every delta. One disagreement
is a sampling race (0.02% on live data); `--divergence-tolerance` consecutive
disagreements mean the book has actually drifted, and it stops being quotable
until the next snapshot rebuilds it.

`best_bid_ask` events are sampled at their own instant and disagreed on 31% of
reads in the same recording, so they are reported as an observation and never
reject a book. See `ARCHITECTURE.md` for the measurements.

The report gives duration, event counts, gaps, feed latency, spread and depth
statistics, and a state digest. `--verify` replays the file twice and confirms the
digests match — the same input produces the same book states.

**Fills are never inferred.** Trades are counted only from `last_trade_price`
events. A recorded price touching a quote proves nothing about queue position.

## Queue-conservative simulator

```bash
python -m src.simulate recordings/<recording>           # one assumption point
python -m src.simulate recordings/<recording> --grid    # sweep the assumptions
```

Replays a recording and maintains simulated resting orders alongside the
reconstructed book. Nothing is submitted; no credentials are read.

Every unresolvable question is answered against the strategy: all resting size at
our price is ahead of us, cancels never move us up, orders are not live until the
placement latency has passed, a cancel does not protect us until its own latency
has passed, and fills are credited only from recorded trades whose taker side
consumed our side. Leftover inventory is sold into the bid as a taker. Liquidity
rewards are never credited — only eligible quoting time is reported.

Queue position cannot be recovered from public data, so `--grid` sweeps latency,
queue depth and the complementary-fill assumption. The worst corner of that range
is the result.

See `ARCHITECTURE.md` for what it found on the first recordings.

## Design notes

`ARCHITECTURE.md` covers the recorder, replay and simulator design: the on-disk
format, the quotability state machine, the conservative fill model, the
invariants, the threading model, the measured findings, and the known limits.

## Using a coding agent

If you're using Claude Code, Cursor, or ChatGPT to follow along, drop the agent into this directory. The agent will read `CLAUDE.md` automatically (Claude Code does this without being asked) and that file points at all the reference material under `docs/`. The agent then has direct access to:

- Every official Polymarket API doc as Markdown
- Comprehensive RTDS + Coinbase WebSocket references
- A `gotchas.md` file documenting every common bug we've watched beginners hit

This is the difference between an agent that hallucinates API surfaces and an agent that grounds itself in real docs.

## Tutorial link

Full step-by-step guide: <https://polyresearchrobotics.com/guide/how-to-build-a-polymarket-trading-bot>
