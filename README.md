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

## Using a coding agent

If you're using Claude Code, Cursor, or ChatGPT to follow along, drop the agent into this directory. The agent will read `CLAUDE.md` automatically (Claude Code does this without being asked) and that file points at all the reference material under `docs/`. The agent then has direct access to:

- Every official Polymarket API doc as Markdown
- Comprehensive RTDS + Coinbase WebSocket references
- A `gotchas.md` file documenting every common bug we've watched beginners hit

This is the difference between an agent that hallucinates API surfaces and an agent that grounds itself in real docs.

## Tutorial link

Full step-by-step guide: <https://polyresearchrobotics.com/guide/how-to-build-a-polymarket-trading-bot>
