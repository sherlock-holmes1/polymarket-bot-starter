# Coinbase Advanced Trade WebSocket — Full Reference

Source: <https://docs.cdp.coinbase.com/exchange/docs/ws-overview> and the Advanced Trade WS overview.

Coinbase exposes a free, public WebSocket for real-time market data on every spot pair Coinbase Advanced Trade lists. For a Polymarket bot, the **`ticker` channel on `BTC-USD`** is the useful one: it pushes a price update every time a trade prints. No API key, no auth, no rate-limit issues for read-only channels.

Use Coinbase as a **fallback** to Polymarket's RTDS Chainlink stream. Coinbase tracks Chainlink closely (sub-second drift in normal conditions), so it's a good safety net when RTDS is unreachable. For primary signal generation on 15M BTC markets, prefer RTDS — the market resolves against Chainlink, not Coinbase.

---

## Endpoint

```
wss://advanced-trade-ws.coinbase.com
```

Public market-data channels (`ticker`, `ticker_batch`, `market_trades`, `level2`, `candles`, `status`, `heartbeats`) require no authentication.

Private channels (`user`, `futures_balance_summary`) require a signed JWT — not relevant to a Polymarket-only bot.

---

## Subscription protocol

### Subscribe

```json
{
  "type": "subscribe",
  "product_ids": ["BTC-USD"],
  "channel": "ticker"
}
```

Multiple product IDs and multiple channels are supported by sending repeated subscribe messages — Coinbase does **not** allow combining multiple channels in one subscribe (each subscribe targets exactly one channel).

### Unsubscribe

```json
{
  "type": "unsubscribe",
  "product_ids": ["BTC-USD"],
  "channel": "ticker"
}
```

### Keep-alive

Coinbase uses the standard **WebSocket ping/pong protocol frames**, not application-level PING strings. With the `websocket-client` Python library, set `ping_interval=20, ping_timeout=10` on `run_forever()` and you're done. The library sends protocol-level ping frames; Coinbase responds with pong frames.

---

## Channel: `ticker` (RECOMMENDED for price feed)

Emits one message per trade. Each message includes the trade price, size, and the current best bid/ask.

### Message shape

```json
{
  "channel": "ticker",
  "client_id": "",
  "timestamp": "2026-05-12T23:34:01.981Z",
  "sequence_num": 0,
  "events": [
    {
      "type": "snapshot",
      "tickers": [
        {
          "type": "ticker",
          "product_id": "BTC-USD",
          "price": "67234.50",
          "volume_24_h": "12345.67",
          "low_24_h": "66500.00",
          "high_24_h": "67800.00",
          "low_52_w": "30000.00",
          "high_52_w": "75000.00",
          "price_percent_chg_24_h": "1.23",
          "best_bid": "67234.40",
          "best_bid_quantity": "0.5",
          "best_ask": "67234.60",
          "best_ask_quantity": "0.3"
        }
      ]
    }
  ]
}
```

The first event after subscribe is `type: "snapshot"` with the current state. Subsequent events are `type: "update"` with the same ticker structure.

Fields most relevant to a price-feed loop: `price`, `best_bid`, `best_ask`, and the wall-clock `timestamp`.

### Subscribe + read in Python

```python
import json, threading, time
from websocket import WebSocketApp

URL = "wss://advanced-trade-ws.coinbase.com"

def on_open(ws):
    ws.send(json.dumps({
        "type": "subscribe",
        "product_ids": ["BTC-USD"],
        "channel": "ticker",
    }))

def on_message(_ws, raw):
    msg = json.loads(raw)
    if msg.get("channel") != "ticker":
        return
    for event in msg.get("events", []):
        for t in event.get("tickers", []):
            if t.get("product_id") == "BTC-USD":
                price = float(t["price"])
                # do something with price

ws = WebSocketApp(URL, on_open=on_open, on_message=on_message)
ws.run_forever(ping_interval=20, ping_timeout=10)
```

This is the same pattern used in `src/price_feed.py:_coinbase_loop()` of the starter project.

---

## Channel: `ticker_batch`

Same payload as `ticker` but throttled to one message every 5 seconds, aggregating trades that happened in between. Use it if you don't need per-trade granularity and want lower message volume.

```json
{ "type": "subscribe", "product_ids": ["BTC-USD"], "channel": "ticker_batch" }
```

---

## Channel: `market_trades`

Streams individual trades as they happen on Coinbase Advanced Trade. Each message includes trade ID, price, size, side, and timestamp. Useful if you want trade-by-trade tape rather than aggregated tickers.

```json
{ "type": "subscribe", "product_ids": ["BTC-USD"], "channel": "market_trades" }
```

---

## Channel: `level2`

Full Level 2 orderbook updates — bid/ask quantities at every price level. **Heavy traffic**, only subscribe if you actually need depth-of-book. For a Polymarket bot the level1 best bid/ask in `ticker` is usually enough.

```json
{ "type": "subscribe", "product_ids": ["BTC-USD"], "channel": "level2" }
```

---

## Channel: `candles`

OHLCV candles on a few standard intervals. Subscribe with `granularity` to pick the timeframe.

```json
{
  "type": "subscribe",
  "product_ids": ["BTC-USD"],
  "channel": "candles",
  "granularity": "FIVE_MINUTE"
}
```

Granularity options: `ONE_MINUTE`, `FIVE_MINUTE`, `FIFTEEN_MINUTE`, `THIRTY_MINUTE`, `ONE_HOUR`, `TWO_HOUR`, `SIX_HOUR`, `ONE_DAY`.

---

## Channel: `status` and `heartbeats`

`status` notifies when products are added or removed. `heartbeats` sends a periodic ping you can use as a liveness check independent of WebSocket-protocol pings.

---

## Reconnection

Connections drop on idle, maintenance, or network blips. The standard pattern is a `while True` loop around `ws.run_forever()` with a 2-second back-off, exactly as in `src/price_feed.py:_coinbase_loop()`. On reconnect, the library re-emits `on_open` and your subscribe message goes out automatically — Coinbase does not persist your subscriptions across reconnects.

---

## Rate limits

Coinbase Advanced Trade WS has no hard public message-rate limit on **read-only market data channels**. There's an implicit limit on number of subscribe/unsubscribe messages per minute (≈8–10). Don't churn subscriptions in a hot loop.

---

## When to prefer Coinbase over Polymarket RTDS

- RTDS endpoint is unreachable from your network (rare but possible).
- You want a sanity-check feed running in parallel as a divergence alarm.
- You're testing your bot's signal logic and don't want to register for a sponsored Chainlink key.

In every other case, prefer `crypto_prices_chainlink` on RTDS — same source the market resolves against.
