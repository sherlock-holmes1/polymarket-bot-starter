# Architecture — read-only market-data recorder

This document covers the recorder and replay path only: `market_spec.py`,
`collector.py`, `market_channel.py`, `recording.py`, `orderbook.py`, `replay.py`.
The tutorial's trading modules (`signal_engine.py`, `orders.py`, `risk.py`,
`scheduler.py`) are out of scope and unchanged.

Purpose: produce recordings of public order-book data that a quoting simulator
can be trusted to run against. Trust is the whole point — a recording that looks
complete but silently lost updates would poison every result downstream. Most of
the design below exists to make that failure visible rather than invisible.

No credentials, no signing, no order submission. Read-only collection stays
permitted where trading is not.

---

## 1. Shape

Two programs with one file format between them.

```
  WRITE                                       READ

  market_spec.py   select a market,
                   capture its config
         │
         ▼
  market_channel.py  WebSocket + reconnect
         │
         ▼
  collector.py     wire them, track gaps      orderbook.py  rebuild + judge books
         │                                            ▲
         ▼                                            │
  recording.py     append-only JSONL          replay.py     CLI + report
         │                                            │
         └──────►  recordings/<name>/  ───────────────┘
                     metadata.json
                     events.jsonl
```

The split is load-bearing:

- **The collector never interprets.** It resolves a market, subscribes, and
  writes payloads verbatim. It builds no order book and makes no judgement about
  the data. Its only derived state is gap bookkeeping: which tokens still owe a
  snapshot, and whether a gap is currently open.
- **The replay never touches the network.** `orderbook.replay()` is a pure
  function from an iterable of rows to a report. No clock, no file handles, no
  sockets.

That purity is what makes determinism testable, and what lets every test drive
the analysis with literal dicts instead of a fixture server.

---

## 2. On-disk format

A recording is a directory containing two files. It is append-only and never
rewritten. `JsonlRecorder` opens with mode `"x"`, so a directory that already
exists is a hard error, never a silent overwrite.

### `metadata.json`

The chosen market's full public trading configuration, as observed, with the
observation timestamp: outcome token IDs with **verbatim** labels, tick size,
minimum order size, maker and taker base fees, reward configuration, tags,
resolution description. Plus the data sources used and a description of the
clocks.

The raw API payload is *not* here — it lives in the first `market_spec` event,
so `metadata.json` stays readable while nothing is lost.

### `events.jsonl`

One JSON object per line, in arrival order.

| Field | Meaning |
|---|---|
| `sequence` | monotonic per recording, assigned under the writer lock |
| `kind` | row type — see below |
| `payload` | the event, **verbatim** for `market_event` |
| `exchange_timestamp_ms` | the venue's own timestamp, or `null` |
| `received_unix_ms` / `received_at` | local wall clock at receipt |
| `received_monotonic_ns` | local monotonic clock at receipt |

Three clocks, because they answer different questions. Venue time says when the
venue thinks it happened. Wall clock says when we learned about it. Monotonic
time survives an NTP step mid-recording, which wall clock does not.

`kind` is one of:

| Kind | Emitted when |
|---|---|
| `market_spec` | the market's configuration is observed or re-observed |
| `collector_started` / `collector_stopped` | process lifecycle |
| `connection` | `connecting`, `subscribed`, `disconnected` |
| `market_event` | anything the market channel sent, unmodified |
| `collector_gap` | `opened` on a disconnect, `closed` once every token re-snapshots |

---

## 3. Modules

### `market_spec.py` — selecting a market on purpose

`MarketSpec` is the venue's complete public answer to "how does this market
trade". It keeps `raw`, the untouched API payload, so an unparsed field is never
lost.

Three selection paths converge on it:

| Path | Mechanism |
|---|---|
| `select_market(condition_id=…)` | CLOB `get_market()` directly |
| `select_market(slug=…)` | Gamma resolves slug → condition ID, then CLOB |
| `select_active_btc_updown_15m()` | derives the current window slug, then as above |
| `list_reward_eligible_markets()` | CLOB `/sampling-markets` — the reward-enabled set |

`Outcome.label` stores the venue's spelling verbatim: `Up`/`Down` on a crypto
market, `Yes`/`No` on an election. Normalising these away loses information the
milestone requires be preserved.

`current_btc_updown_15m_slug()` is `now - (now % 900)`. The 15M series names each
window by its own start epoch, so the active slug is derivable from the clock and
needs no search. Two facts about this series were wrong in the starter and are
documented in `docs/gotchas.md`: the slug prefix is `btc-updown-15m`, not
`btc-up-or-down-15m`; and `end_date_iso` is the calendar day, not the window end
(`markets.window_end_iso()` derives the real one).

### `recording.py` — append-only writer

`JsonlRecorder.record()` stamps the three clocks, extracts the venue timestamp
(including from nested `price_changes` when the top level omits it), serialises,
and flushes. Sequence assignment and the write happen under one lock, so rows
from the WebSocket thread and the supervisor thread cannot interleave.

Flushing on every row costs throughput and buys the guarantee that a killed
process still leaves a readable file up to the last event.

### `market_channel.py` — subscription and reconnect

A supervisor around `WebSocketApp` that reopens on **either** trigger: the token
IDs changed (cycle roll), or the connection closed. The starter only handled the
first, so a dropped socket looked like a quiet market indefinitely.

Reconnect is event-driven, not polled. `_on_close` sets a `threading.Event`;
`_supervisor_loop` waits on it with the poll interval as a *timeout*. A close is
acted on immediately rather than up to `--poll-interval` seconds later.
`_failed_attempts` gives exponential backoff (1s→30s), reset to zero on a
successful subscribe so a healthy reconnect is instant.

**The generation counter is the subtle part.** `_reopen` increments
`self._generation` *before* closing the old socket. Each socket's `on_close`
compares its captured generation against the current one and returns early if
stale. A socket closed on purpose therefore cannot report itself as an unexpected
disconnect. Get this order wrong and every 15-minute cycle roll fabricates a data
gap.

Every resubscribe produces a fresh book snapshot, which is what lets the collector
close a gap.

### `collector.py` — wiring and gap tracking

The only stateful component. `_on_connection_state` opens a gap on `disconnected`
and resets `_pending_snapshot_assets` to **every** subscribed token.
`_on_market_event` discards tokens as their snapshots arrive and closes the gap
only when that set empties.

Resetting the full set on disconnect is required, not defensive: without it the
set is already empty from before the drop, so the first token's snapshot closes
the gap while the other book is still stale.

`_current_spec()` re-observes the market config on a timer *and* on a
`market_rolled` predicate. The predicate is local arithmetic — compare the
derived window slug against the recorded one — so following a rolling market
costs no requests until it actually rolls.

### `orderbook.py` — rebuild and judge

`OrderBook.reject_reason()` is the core of the whole system. Everything else in
the file serves it.

```
                    ┌─ no_snapshot   no book snapshot since the last invalidation
                    ├─ gap_open      the recorder was disconnected
  quotable  ◄───────┼─ empty_side    one side has no levels
                    ├─ crossed       best_bid >= best_ask
                    ├─ stale         last update older than --max-stale-ms
                    └─ diverged      sustained disagreement with the venue's top
```

Rejection states are sticky where they should be: `gap_open`, `no_snapshot` and
`diverged` persist until a fresh snapshot rebuilds the book. `crossed`,
`empty_side` and `stale` are evaluated from current state.

`_StateClock` accounts for **time**, not events. On each row it adds the elapsed
interval to every asset's current state bucket, splitting an interval when a book
crosses the staleness boundary part-way through. This is why the report can say
"quotable 38% of the recording" as a real duration rather than an event ratio —
and why a recording that looks busy but was unusable cannot pass as good data.

### `replay.py` — presentation only

Load, replay, optionally replay a second time for `--verify`, render. `_render`
contains no logic, so the human-readable and `--json` outputs cannot disagree.
Exit code 1 on a determinism failure, so it is usable in CI.

---

## 4. Invariants

These are the rules the code is built to hold. Breaking one silently is the
failure mode this architecture exists to prevent.

1. **Payloads are stored verbatim.** Parsing happens at replay time only. A field
   we do not understand today is still in the file tomorrow.
2. **Both clocks are recorded, and they are not interchangeable.** Book freshness
   is measured on *receipt* time; feed latency is measured on *venue* time.
3. **A gap closes only when every subscribed token has re-snapshotted.**
4. **A deliberate reconnect is not a disconnect.** Only a close we did not ask for
   opens a gap.
5. **Fills are never inferred.** Trades are counted only from `last_trade_price`.
   A recorded price touching a quote proves nothing about queue position. The
   report states `fills_inferred: false` explicitly.
6. **Outcome labels are preserved as the venue spells them.**

### Why staleness uses receipt time

A `book` snapshot's `timestamp` is the last book *change*, not the emit time. On
a quiet market the snapshot arrives already "ten seconds old". Measuring freshness
against it made a healthy live market report 38% quotable when it was 100%. Our
knowledge of the book is only as current as the moment the message reached us.

The venue timestamp is still recorded and used — for latency statistics and
out-of-order detection — but only over `price_change`, `last_trade_price`,
`best_bid_ask` and `tick_size_change`, never over snapshots.

### Why determinism is not enough

`--verify` replays the same file twice and compares a digest over every book
state. That proves the analysis is reproducible. It does **not** prove the
recording is complete: a recorder that silently drops deltas replays perfectly
deterministically and produces a confidently wrong book.

`check_against_venue_top()` closes that hole. Every `price_change` entry carries
the venue's own best bid and ask **for that update**, so the locally rebuilt book
is compared against it in-band on every delta. A single disagreement is a sampling
race, not corruption, so a book is rejected only after `--divergence-tolerance`
consecutive disagreements; a fresh snapshot clears the counter. Mismatches are
always counted and warned about, even below the threshold.

Two sources of venue top-of-book exist, and they are **not** interchangeable.
Measured on the same 70-second BTC 15M recording:

| Source | Checks | Disagreements | Rate |
|---|---|---|---|
| `price_change` (in-band with the update) | 22,290 | 4 | 0.02% |
| `best_bid_ask` (sampled at its own instant) | 58 | 18 | 31% |

`best_bid_ask` is therefore recorded and reported as an **observation** and never
feeds the drift counter. At 31% noise, three consecutive disagreements are
routine, so including it would false-invalidate healthy books at the default
tolerance. This distinction was found by measuring, not by reading the docs —
both fields are described identically upstream.

---

## 5. Concurrency

Three threads, with a clean boundary between them.

| Thread | Runs | Touches |
|---|---|---|
| main | signal handling, `--duration` deadline | start/stop only |
| supervisor | `_supervisor_loop` — market lookup, reconnect decisions | collector state under lock |
| websocket | `run_forever` per connection, one at a time | collector state under lock |

`PublicMarketCollector._lock` guards `_spec`, `_subscribed_assets`,
`_pending_snapshot_assets` and `_gap_open`. `JsonlRecorder._lock` separately
guards sequence assignment and the file write.

Network I/O in `_current_spec()` happens deliberately **outside** the collector
lock. Holding a lock across an HTTP call would stall the WebSocket thread's event
recording for the duration of a retry chain.

The replay path is single-threaded and has no locks, by construction.

---

## 6. Known limits

- **`new_market` and `market_resolved` are venue-wide broadcasts.** With
  `custom_feature_enabled: true` these arrive for every market on Polymarket —
  131 in a 70-second recording of one market. They are recorded but excluded from
  the subscribed market's statistics. They carry `fee_schedule` for newly created
  markets, so the data is available if a fee-config harvester is ever wanted.
- **`--divergence-tolerance 3` is calibrated on one recording** of a liquid
  market (4 in-band disagreements in 22,290 checks, none consecutive). A thinner
  or faster book may race more often and want a higher value.
- **The `best_bid_ask` disagreement rate is itself worth watching.** It is
  reported per asset. A sharp rise is a signal about the feed even though it never
  rejects a book.
- **No queue position.** Public data cannot reveal it. This is a constraint on
  the simulator that consumes these recordings, not on the recorder.
- **One market per recording.** Recording several in parallel means several
  processes. The format would support multiplexing; the collector does not.
- **Authenticated user-channel streams are out of scope** and require separate
  explicit approval.

## 7. Extension points

The next milestone — paper orders and a queue-conservative fill simulator —
consumes recordings rather than modifying the recorder.

- `replay()` already yields per-asset books over time; a simulator subscribes to
  the same event walk and adds its own order state.
- The complementary-pair cost (`best_ask(A) + best_ask(B)`, the number that
  decides whether a two-sided quote is worth posting) is deliberately **not**
  computed here. It is a strategy question, and it belongs to the simulator.
- `MarketSpec` already carries the fee and reward configuration the cost model
  needs. Note that the BTC 15M market publishes a reward *configuration*
  (`min_size`, `max_spread`) but a daily rate of **0** — it is not currently in
  the reward pool. Universe selection must check the rate, not merely the presence
  of a config.
