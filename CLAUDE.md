# Polymarket Bot — Coding Agent Context

You are helping the human build a Python trading bot for Polymarket in 2026. This file gives you everything you need to ground yourself in real Polymarket APIs and avoid the hallucinations that wreck most LLM-generated Polymarket code.

## Where to look first

When the human asks anything Polymarket-specific, do this BEFORE writing code:

1. Open `docs/gotchas.md`. Most beginner bugs are documented there.
2. Search `docs/polymarket/_llms.txt` for the relevant page, then read the matching `docs/polymarket/<path>.md`.
3. For WebSockets specifically, the canonical references are `docs/price-feeds/polymarket-rtds-reference.md` and `docs/price-feeds/coinbase-advanced-trade-ws.md`.
4. If you still aren't sure, read `docs/polymarket/_llms-full.txt` — it's the entire docs site concatenated into one Markdown file.

If you can't find the answer in `docs/`, say so. Do not guess Polymarket API surfaces from training data — Polymarket changes endpoint shapes between SDK versions and the docs in this folder are the ground truth.

## Project facts

- **Network**: Polygon mainnet, chain ID 137. Not Ethereum mainnet.
- **Collateral**: pUSD (ERC-20 wrapper, 1:1 with USDC). Acquire by wrapping USDC.e via Polymarket's CollateralOnramp contract, or by depositing through the official bridge (auto-wraps).
- **CLOB REST API**: `https://clob.polymarket.com`
- **CLOB Market WebSocket**: `wss://ws-subscriptions-clob.polymarket.com/ws/market` (subscribe by token/asset IDs)
- **CLOB User WebSocket**: `wss://ws-subscriptions-clob.polymarket.com/ws/user` (for the human's own order/trade events)
- **Real-Time Data Socket (RTDS)**: `wss://ws-live-data.polymarket.com` (crypto prices, equity prices, comments)
- **Python SDK**: `py-clob-client-v2`. V1 `py-clob-client` is deprecated.
- **Resolution mechanism**: UMA Optimistic Oracle. For BTC Up/Down 15M markets, the *price* feeding the proposer is **Chainlink BTC/USD**, not Binance, not Coinbase.

## Market terminology (CRITICAL)

For the BTC Up/Down 15M market and similar crypto Up/Down markets, the outcomes are **UP** and **DOWN**, not YES and NO. The CLOB API exposes them as two token IDs per market; the user-facing labels in the Polymarket UI are "Up" and "Down". When writing prose, comments, or variable names, use UP/DOWN. Other Polymarket markets (e.g. elections) use YES/NO — keep the terminology per-market.

## Always-true rules

1. **Active market lookup must refresh every cycle.** A new BTC Up/Down 15M market opens every 15 minutes with a fresh `condition_id` and fresh token IDs. Hardcoding a condition ID works for one cycle and silently fails for every subsequent cycle. Use `src/markets.get_active_btc_15m_market()`.
2. **The CLOB market WebSocket must resubscribe each cycle.** When the cycle rolls, the old token IDs are dead. Unsubscribe (or close + reopen) and subscribe to the new market's token IDs. `src/market_channel.py` handles this automatically — re-use that pattern.
3. **Polymarket RTDS Chainlink BTC is a single, stable feed.** It doesn't roll with market cycles — `crypto_prices_chainlink` with `{"symbol":"btc/usd"}` always emits the same Chainlink BTC/USD stream. No resubscribe needed there.
4. **RTDS keep-alive**: send the literal text `PING` every 5 seconds. Not a JSON message, not a WebSocket ping frame — a plain text `PING`. See `docs/price-feeds/polymarket-rtds-reference.md`.
5. **Tick size**: round limit prices to 0.01 (two decimals) for most Polymarket markets. Submitting an off-tick price returns a 400.
6. **Probabilities, not dollars**: order prices are decimal probabilities in [0.01, 0.99], not USD amounts. A "price" of 0.58 means you pay $0.58 per share and win $1.00 if the share resolves UP.
7. **Rate limits are real**: use exponential back-off on 429.
8. **Never log private keys or full API secrets.** Mask them in logs.

## When the human writes new code

- Add type hints throughout — `from __future__ import annotations` is fine.
- Use the existing logger from `src.utils.get_logger`.
- Don't write `try: ... except: pass`. If you catch, log with context and re-raise or return a sentinel that callers handle.
- Don't reach for `time.sleep` in async code. The bot mixes a synchronous main loop with WebSocket threads — keep that boundary clean.
- Don't introduce new dependencies without asking. The current dep set is in `requirements.txt`.

## When the human is stuck

- Quote their error log back at them in plain English, then point to the exact `docs/polymarket/...` page that explains the failing endpoint.
- If their bot is "not getting fills," ask in order: is the price on-tick? Is the size above the per-market minimum? Is the order book actually liquid right now (check spread)? Is the wallet funded?
- If their WebSocket "keeps disconnecting," check: are they sending `PING` every 5s on RTDS? Are they handling close codes 1000/1001 vs unexpected disconnects?

## Pacing

- Don't over-explain. The human is following a paginated tutorial — keep your explanations tight and tied to the step they're on.
- Don't add features they didn't ask for. If they ask to add a feature, add only that.
- After writing code, point at the *specific* lines that implement what they asked for. They'll move faster if they can navigate by line number.
