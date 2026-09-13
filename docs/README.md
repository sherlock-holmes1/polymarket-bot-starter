# Reference Documentation

Drop-in reference material for the coding agent and human builder. If you're using Claude Code, Cursor, or ChatGPT, point the agent at this folder — see `../CLAUDE.md` for how the agent should use it.

## What's in here

- **`gotchas.md`** — Hard-won knowledge: bugs everyone hits and how the starter avoids them. Read this first.
- **`price-feeds/polymarket-rtds-reference.md`** — Comprehensive Polymarket Real-Time Data Socket reference (Chainlink BTC/USD, Binance, Pyth equity prices, comments). Protocol, every subscription type, every payload shape, keep-alive, reconnect strategy.
- **`price-feeds/coinbase-advanced-trade-ws.md`** — Comprehensive Coinbase Advanced Trade WebSocket reference (ticker, ticker_batch, market_trades, level2, candles). Used as the fallback price feed in `src/price_feed.py`.
- **`polymarket/`** — Mirror of `docs.polymarket.com` as Markdown. 161 pages.
  - `_llms.txt` — Index of all pages with one-line descriptions.
  - `_llms-full.txt` — Every page concatenated into one Markdown file. Good for stuffing into a single LLM context window.
  - `_manifest.json` — Generation metadata and file list.
  - `api-reference/`, `concepts/`, `market-data/`, `market-makers/`, `trading/`, `builders/`, `advanced/`, `resources/` — Mirrored directory layout.
