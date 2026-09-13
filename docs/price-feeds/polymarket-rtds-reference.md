# Polymarket Real-Time Data Socket (RTDS) — Full Reference

Source: <https://docs.polymarket.com/market-data/websocket/rtds> (mirrored at `docs/polymarket/market-data/websocket/rtds.md`).

The RTDS is Polymarket's WebSocket service for streaming **crypto prices**, **equity prices**, and **comments**. For a trading bot, the crypto-prices channel is the important one — specifically the **Chainlink BTC/USD** stream, because that's the price feed Polymarket's BTC Up/Down 15M markets resolve against.

This reference is the everything-you-need version: protocol, every subscription type, every payload shape, keep-alive handling, error modes.

---

## Endpoint

```
wss://ws-live-data.polymarket.com
```

No authentication is required for the public streams (crypto prices from Binance and Chainlink, equity prices from Pyth, comments). Some user-specific streams may require a `gamma_auth` block with your wallet address.

For 15-minute crypto markets, you can optionally request a **sponsored Chainlink API key** from Chainlink, with onboarding support, via <https://pm-ds-request.streams.chain.link/>. This gets you priority/SLA on the Chainlink stream; the free stream still works without it.

---

## Subscription protocol

Subscriptions are JSON messages. Send a `subscribe` action to start receiving updates; send `unsubscribe` to stop. You can add, remove, and modify subscriptions on a live connection without disconnecting.

### Subscribe envelope

```json
{
  "action": "subscribe",
  "subscriptions": [
    {
      "topic": "topic_name",
      "type": "message_type_or_*",
      "filters": "optional_filter_string",
      "gamma_auth": { "address": "wallet_address" }
    }
  ]
}
```

### Unsubscribe

Same shape with `"action": "unsubscribe"`.

### Keep-alive — CRITICAL

Send the literal text `PING` (not a JSON message, not a WebSocket protocol-level ping frame) every **5 seconds**. The server replies with `PONG`. Connections that don't PING are closed after ~10 seconds of silence.

```python
# Inside your on_open handler
def ping_loop(ws):
    while ws.keep_running:
        ws.send("PING")
        time.sleep(5)
threading.Thread(target=ping_loop, args=(ws,), daemon=True).start()
```

---

## Message envelope

Every server-sent message follows:

```json
{
  "topic": "string",
  "type": "string",
  "timestamp": 1753314064237,
  "payload": { /* topic-specific */ }
}
```

| Field       | Type   | Notes                                                                       |
| ----------- | ------ | --------------------------------------------------------------------------- |
| `topic`     | string | The subscription topic. E.g. `crypto_prices`, `crypto_prices_chainlink`, `equity_prices`, `comments` |
| `type`      | string | Event type within the topic. E.g. `update`, `reaction_created`              |
| `timestamp` | number | Unix milliseconds when the server sent the message                          |
| `payload`   | object | Topic-specific event data                                                   |

---

## Topic: `crypto_prices_chainlink` (RECOMMENDED for BTC bots)

Chainlink BTC/USD is the price feed that Polymarket's 15-minute BTC Up/Down markets resolve against. Subscribing to this topic means your bot's view of "the price" is the *same* view the UMA resolution proposer will use at expiry — eliminating basis risk.

### Subscribe — all Chainlink symbols

```json
{
  "action": "subscribe",
  "subscriptions": [
    {
      "topic": "crypto_prices_chainlink",
      "type": "*",
      "filters": ""
    }
  ]
}
```

### Subscribe — specific symbol (BTC/USD)

```json
{
  "action": "subscribe",
  "subscriptions": [
    {
      "topic": "crypto_prices_chainlink",
      "type": "*",
      "filters": "{\"symbol\":\"btc/usd\"}"
    }
  ]
}
```

Symbol format is **slash-separated**: `btc/usd`, `eth/usd`, `sol/usd`, `xrp/usd`.

### Update payload — Bitcoin

```json
{
  "topic": "crypto_prices_chainlink",
  "type": "update",
  "timestamp": 1753314088421,
  "payload": {
    "symbol": "btc/usd",
    "timestamp": 1753314088395,
    "value": 67234.50
  }
}
```

| Field       | Type   | Notes                                              |
| ----------- | ------ | -------------------------------------------------- |
| `symbol`    | string | Slash-separated pair name                          |
| `timestamp` | number | When Chainlink recorded the price (ms since epoch) |
| `value`     | number | Current price in the quote currency                |

### Supported Chainlink symbols

`btc/usd`, `eth/usd`, `sol/usd`, `xrp/usd`.

---

## Topic: `crypto_prices` (Binance source)

A second crypto stream sourced from Binance. **Don't use this as your primary signal source for 15M crypto markets** — the markets resolve against Chainlink, not Binance, so there's basis risk.

### Subscribe — all Binance symbols

```json
{
  "action": "subscribe",
  "subscriptions": [
    { "topic": "crypto_prices", "type": "update" }
  ]
}
```

### Subscribe — specific symbols (BTC, ETH, SOL)

```json
{
  "action": "subscribe",
  "subscriptions": [
    {
      "topic": "crypto_prices",
      "type": "update",
      "filters": "solusdt,btcusdt,ethusdt"
    }
  ]
}
```

Symbol format is **lowercase concatenated**: `btcusdt`, `ethusdt`, `solusdt`, `xrpusdt`. Filter is a comma-separated string (no JSON).

### Update payload — Bitcoin

```json
{
  "topic": "crypto_prices",
  "type": "update",
  "timestamp": 1753314088421,
  "payload": {
    "symbol": "btcusdt",
    "timestamp": 1753314088395,
    "value": 67234.50
  }
}
```

---

## Topic: `equity_prices` (Pyth source)

Real-time prices for stocks, ETFs, forex, precious metals, and commodities, sourced from Pyth. A 30-day free tier is available; ongoing access is $99/month. <https://buy.stripe.com/cNi8wPeiq76FgQrbsD4ZG09>.

All asset classes flow through one `equity_prices` topic. Subscribing with a symbol filter triggers a **historical snapshot of the last 2 minutes**, then continues live.

### Subscribe

```json
{
  "action": "subscribe",
  "subscriptions": [
    {
      "topic": "equity_prices",
      "type": "update",
      "filters": "{\"symbol\":\"AAPL\"}"
    }
  ]
}
```

Symbols use standard tickers (`AAPL`, `SPY`, `EUR/USD`, `XAU/USD`, etc.).

---

## Topic: `comments`

Real-time market commentary events. Not needed for a trading bot — included for completeness.

---

## Error and lifecycle messages

When something goes wrong (invalid subscription, malformed filter, rate limit), the server returns a JSON message with `type: "error"`:

```json
{
  "topic": "error",
  "type": "error",
  "timestamp": 1753314088421,
  "payload": {
    "code": "INVALID_FILTER",
    "message": "Filter could not be parsed as JSON"
  }
}
```

Log these. Do not retry the bad subscription — fix the payload.

---

## Reconnect strategy

When the connection drops, reconnect with **exponential back-off** starting at 2 seconds, capped at 30 seconds. On reconnect, re-send all your subscriptions — the server doesn't remember them. Restart the PING loop too.

Reference implementation: `src/price_feed.py:_rtds_loop()` in this starter project.

---

## Reference client

Polymarket maintains an official TypeScript client: <https://github.com/Polymarket/real-time-data-client>. There is no official Python client; the starter project's `src/price_feed.py` uses `websocket-client` directly.
