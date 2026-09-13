# Polymarket Bot Gotchas

Hard-won knowledge from people who've built Polymarket bots. Read this before you start. If your bot is broken and the failure isn't obvious, scan this file first — most beginner bugs are in here.

The starter project's source code already implements the fixes for the gotchas marked `[fixed]`. For those, this doc explains *why* the code looks the way it does — so when you (or your coding agent) refactor, you don't accidentally regress.

---

## Markets and cycles

### `[fixed]` The BTC Up/Down 15M market rolls every 15 minutes — never hardcode the condition ID

A new BTC Up/Down 15M market opens every 15 minutes with a fresh `condition_id` and fresh UP / DOWN token IDs. The old market resolves and stops accepting orders. If your bot hardcodes `condition_id`, it places exactly one cycle of orders successfully and then silently does nothing forever after.

**How the starter handles it:** `src/markets.get_active_btc_15m_market(client)` re-resolves the window on every cycle — it derives the current window's slug from the clock, resolves it to a `condition_id`, and confirms the market is still accepting orders before returning it.

### `[fixed]` The CLOB market WebSocket must resubscribe at every cycle roll

Because the active token IDs change every 15 minutes, a long-lived subscription to the CLOB market channel sees its events trail off as the cycle rolls. The connection stays open but the orderbook data is for the dead market.

**How the starter handles it:** `src/market_channel.MarketChannelSupervisor` takes a `get_active_token_ids` callable and polls it on a cadence. When the token IDs change, the supervisor tears down the current WS and opens a new one with the new IDs.

### The market briefly disappears between cycles

For 5–15 seconds between an old market resolving and a new one becoming visible in `get_markets()`, your active-market lookup will return nothing. Retry, don't abort.

**How the starter handles it:** `get_active_btc_15m_market` raises a `RuntimeError` with a hint to retry. The scheduler loop catches it and waits one cycle.

### `[fixed]` The BTC Up/Down 15M slug is `btc-updown-15m-<window-start-epoch>`

Not `btc-up-or-down-15m-<date>-<time>`. Each window is named by its own start
epoch, so the active slug is derivable from the clock — no lookup needed to know
what to ask for. Scanning `get_markets()` pages for this series finds nothing;
it does not sit near the front of that listing.

**How the starter handles it:** `src/market_spec.current_btc_updown_15m_slug()`
derives the slug, resolves it to a `condition_id` via the public Gamma API, then
reads the full CLOB market object.

### `end_date_iso` on the 15M series is the calendar day, not the window

A market whose window closes at 3:30PM ET reports `end_date_iso` of
`2026-09-13T00:00:00Z`. Computing a cycle deadline from it is wrong by up to 24
hours. The window end is the slug's epoch plus 900 seconds —
`src/markets.window_end_iso()` does this.

### `new_market` and `market_resolved` are venue-wide broadcasts

With `custom_feature_enabled: true` you receive these for *every* market on
Polymarket, not just the asset IDs you subscribed to. In a 70-second recording of
one market, 131 of them arrived for unrelated 5-minute crypto and sports markets.
Filter by your own condition ID before counting them as your market's events.

### A `book` snapshot's `timestamp` is the last book change, not the emit time

Subtracting it from local receipt time reads as tens of seconds of "latency" on a
quiet market. Measure feed latency on `price_change` and `last_trade_price` only.
Measure book *freshness* on your own receipt clock — that is what your knowledge
of the book is actually worth.

### Question text changes more often than the slug pattern

Filter on `market_slug` (e.g. `btc-up-or-down-15m-2026-05-12-1400`) rather than fuzzy-matching the question text. Polymarket has changed question wording mid-series before — slugs are more stable.

---

## Authentication

### L1 (private key) vs L2 (API key) — both are needed for trading

CLOB **read** endpoints (orderbook, prices, markets) need nothing. CLOB **trade** endpoints (place order, cancel) need all 5 `POLY_*` env vars: API_KEY, API_SECRET, API_PASSPHRASE, PRIVATE_KEY, and PROXY_ADDRESS. Missing the proxy address is the #1 cause of "401 Unauthorized" with the correct API key — the key is bound to the proxy address, not the EOA.

### Regenerate API keys after wallet changes

API keys are derived from the wallet that created them. If you change wallets, the old keys go dead silently — every request returns 401 with no helpful detail. Regenerate from the Polymarket UI.

### Don't log credentials

Mask `POLY_API_SECRET`, `POLY_PRIVATE_KEY`, and `CHAINLINK_RTDS_API_KEY` in any log line. The `src.utils.get_logger` default format does not auto-mask — be deliberate.

---

## Orders

### Tick size: round limit prices to 0.01

Polymarket enforces a minimum price increment. For most markets including BTC Up/Down 15M, the tick is 0.01. Submitting `0.587` returns a 400 with a tick-size error. Round **down** (floor) to two decimals before sending — `round_to_tick` in `src/orders.py` does this.

### Order sizes are denominated in pUSD, not shares

When you submit `size=10.0`, you're saying "spend 10 pUSD on shares at the limit price." If your limit price is 0.50, you get 20 shares. Confusing this with "buy 10 shares" leads to 2× sized orders.

### Selling shares you don't own is shorting

Polymarket lets you submit SELL orders for token IDs you don't currently hold (this is how market makers work). For a beginner bot, only ever submit BUY orders. Selling shares to close a position is a different code path — wait until Step 5 covers it.

### Probabilities, not dollars

A limit price of `0.58` means you're offering $0.58 per share. If the market resolves in your favor, you receive $1.00. So a fill at 0.58 returns ($1.00 − $0.58) / $0.58 ≈ 72% gross on the trade. Don't size as if 0.58 were the dollar bet.

---

## WebSockets

### `[fixed]` RTDS keep-alive is the literal string `PING` every 5 seconds

Not a JSON message, not a WebSocket-protocol ping frame. The literal text `PING`, sent as a text message. Servers respond with `PONG` (also literal text). If you don't send it, the connection is closed after ~10 seconds.

**How the starter handles it:** `src/price_feed._rtds_on_open` spawns a 5-second PING loop.

### `[fixed]` Coinbase Advanced Trade WS uses standard ping frames

The `websocket-client` library sends them when you set `ping_interval=20, ping_timeout=10`. No custom protocol needed.

### Always implement reconnect-with-backoff on WebSockets

Both Polymarket and Coinbase will close idle/stale connections. The starter project wraps both in `while True` reconnect loops with a 2-second back-off.

### Initial dump can be skipped if you only want incremental updates

The CLOB market channel sends a full orderbook snapshot on subscribe by default. If you're managing your own delta-replay state, set `initial_dump: false` in the subscription. For most bots, leave it true.

### RTDS Chainlink BTC is a single, stable feed — DO NOT resubscribe at cycle rolls

The `crypto_prices_chainlink` topic with `{"symbol":"btc/usd"}` is the same Chainlink BTC/USD stream regardless of which Polymarket market is currently active. Only the CLOB market channel (which is per-market) needs to be resubscribed.

---

## Resolution

### Markets resolve via UMA Optimistic Oracle — not instantly at the timestamp

When the 15M window closes, the market doesn't immediately pay out. A human (or a bot) proposes the outcome to UMA, posts a $750 pUSD bond, and there's a 2-hour challenge period before resolution finalizes. **Your winning shares are illiquid for up to several hours.** Don't write code that assumes immediate payout.

### The price feeding resolution comes from Chainlink, not Binance

For BTC Up/Down crypto markets, the UMA proposer reads the Chainlink BTC/USD value at the resolution timestamp. The market does **not** resolve against Binance, Coinbase, the Polymarket order book, or any single CEX. Your bot's edge is largely about predicting *that specific Chainlink value*.

### 50/50 resolution is rare but real

If UMA voters can't determine an outcome ("Unknown/50-50"), every token redeems for $0.50. Your bot should not assume binary outcomes when computing PnL — handle the 50/50 case explicitly.

---

## Network / collateral

### Polygon mainnet (chain ID 137) — not Ethereum

If you send USDC from Coinbase or Kraken, **select Polygon as the withdrawal network**. Sending Ethereum-mainnet USDC to your Polymarket deposit address loses the funds (recoverable only via support, slowly). Default chain in MetaMask is Ethereum; switch to Polygon first.

### USDC.e ≠ USDC — and Polymarket trades in pUSD, not either

The collateral token on Polymarket is **pUSD**, an ERC-20 wrapper backed 1:1 by USDC.e on Polygon. To go from raw USDC on Ethereum mainnet to tradeable balance:

1. Bridge to Polygon → arrives as USDC.e
2. Approve and call Polymarket's CollateralOnramp `wrap` → mints pUSD

The official Polymarket deposit bridge auto-wraps if you deposit any supported asset on any supported chain.

---

## Rate limits

### Use exponential back-off on 429 — they're real and aggressive

Polymarket rate-limits both REST and WS aggressively. On a 429 response, back off (1s, 2s, 4s, 8s) and surface the failure to your scheduler rather than retrying tightly. `src.utils.retry_with_backoff` is wired for this.

### Don't poll `get_markets()` more than ~once per cycle

The active-market lookup is the heaviest endpoint you'll call regularly. Cache its result within a cycle. The starter project's scheduler calls it once at cycle start and reuses the result.

---

## Paper trading

### Always validate in paper mode before going live

Set `PAPER_TRADING=true` in `.env`. The `place_limit_order` function will short-circuit to a simulated fill and log to `paper_trades.csv` instead of submitting to the CLOB. Run for at least 24 hours (≈96 cycles) and review the CSV before flipping to live.

### Simulated fills aren't honest about slippage

Paper mode assumes you fill at your limit price. Real fills walk the book and pay the spread. Adjust simulated fills by the current spread to be more realistic, or accept that paper PnL will be slightly rosier than live.

---

## Debugging tips

### Log the full request payload before submission

When `create_and_post_order` returns a 400, the exception message is often vague. Log the full `OrderArgs` you constructed *before* the API call so you can see the exact `token_id`, `price`, `size`, and `side` that were rejected.

### Confirm the wallet you signed with is the wallet the API key is bound to

A common silent failure: you regenerated API credentials in one Polymarket account but `POLY_PRIVATE_KEY` still points at a different wallet. `client.get_ok()` returns success but every order returns 401. Cross-check the derived address from your private key against your `POLY_PROXY_ADDRESS`.

### When the order book looks empty, check the cycle clock

If `get_order_book` returns no bids/asks, it's almost certainly because the current cycle's market just closed and the new one hasn't fully started accepting orders yet. Wait 10–30 seconds and retry.
