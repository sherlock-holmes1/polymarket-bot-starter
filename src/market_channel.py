"""CLOB market-channel WebSocket with cycle-aware resubscription.

The BTC Up/Down 15M market rolls every 15 minutes. When it rolls:
  * The current market's token IDs become dead — orderbook events stop arriving.
  * A new market opens with new UP/DOWN token IDs that have to be subscribed to.

This module wraps the CLOB market WebSocket in a small supervisor that takes a
callable returning the *currently active* market's token IDs, polls it at a
configurable cadence, and tears down + reopens the subscription whenever the
token IDs change. Your trading loop can subscribe to `on_book_update` without
caring about cycle rolls.

Endpoint:   wss://ws-subscriptions-clob.polymarket.com/ws/market
Reference:  docs/polymarket/api-reference/wss/market.md
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable

from websocket import WebSocketApp

from src.utils import get_logger

logger = get_logger(__name__)

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class MarketChannelSupervisor:
    def __init__(
        self,
        get_active_token_ids: Callable[[], tuple[str, str]],
        on_book_update: Callable[[dict], None],
        *,
        poll_interval_s: int = 30,
    ) -> None:
        self._get_token_ids = get_active_token_ids
        self._on_update = on_book_update
        self._poll_interval = poll_interval_s

        self._ws: WebSocketApp | None = None
        self._current_token_ids: tuple[str, str] | None = None
        self._ws_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._supervisor_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def _supervisor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                latest = self._get_token_ids()
            except Exception as exc:
                logger.warning(f"Active-token-id lookup failed: {exc!r}")
                time.sleep(self._poll_interval)
                continue

            if latest != self._current_token_ids:
                logger.info(
                    f"Market cycle rolled — resubscribing CLOB market WS "
                    f"old={self._current_token_ids} new={latest}"
                )
                self._reopen(latest)

            time.sleep(self._poll_interval)

    def _reopen(self, token_ids: tuple[str, str]) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

        self._current_token_ids = token_ids

        def _on_open(ws: WebSocketApp) -> None:
            ws.send(json.dumps({
                "assets_ids": list(token_ids),
                "type": "market",
                "initial_dump": True,
            }))
            logger.info(
                f"CLOB market channel subscribed: up={token_ids[0][:10]}… down={token_ids[1][:10]}…"
            )

        def _on_message(_ws: WebSocketApp, raw: str) -> None:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                return
            try:
                self._on_update(msg)
            except Exception as exc:
                logger.exception(f"on_book_update callback raised: {exc!r}")

        def _on_error(_ws: WebSocketApp, err: Exception) -> None:
            logger.warning(f"CLOB market WS error: {err!r}")

        ws = WebSocketApp(
            MARKET_WS_URL,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
        )
        self._ws = ws

        def _run() -> None:
            ws.run_forever(ping_interval=20, ping_timeout=10)
            if not self._stop.is_set():
                logger.warning("CLOB market WS disconnected (supervisor will resubscribe on next poll)")

        self._ws_thread = threading.Thread(target=_run, daemon=True)
        self._ws_thread.start()
