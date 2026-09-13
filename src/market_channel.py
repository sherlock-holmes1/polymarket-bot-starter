"""CLOB market-channel WebSocket with cycle-aware resubscription and reconnect.

The BTC Up/Down 15M market rolls every 15 minutes. When it rolls:
  * The current market's token IDs become dead — orderbook events stop arriving.
  * A new market opens with new UP/DOWN token IDs that have to be subscribed to.

A rolled cycle is not the only reason to reopen. The venue also closes idle or
stale sockets while the token IDs are unchanged, so the supervisor reconnects on
*either* trigger: a token-ID change, or a closed connection. A close wakes the
supervisor immediately rather than waiting out the poll interval, and repeated
failures back off so a dead endpoint is not hammered.

Every subscribe arrives with a fresh book snapshot, which is what lets a consumer
close the data gap a disconnect opened.

Endpoint:   wss://ws-subscriptions-clob.polymarket.com/ws/market
Reference:  docs/polymarket/market-data/websocket/market-channel.md
"""
from __future__ import annotations

import json
import threading
from typing import Callable, Sequence

from websocket import WebSocketApp

from src.utils import get_logger

logger = get_logger(__name__)

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RECONNECT_BASE_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 30.0


class MarketChannelSupervisor:
    def __init__(
        self,
        get_active_token_ids: Callable[[], Sequence[str]],
        on_book_update: Callable[[dict], None],
        *,
        poll_interval_s: int = 30,
        on_connection_state: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._get_token_ids = get_active_token_ids
        self._on_update = on_book_update
        self._poll_interval = poll_interval_s
        self._on_connection_state = on_connection_state

        self._ws: WebSocketApp | None = None
        self._current_token_ids: tuple[str, ...] | None = None
        self._ws_thread: threading.Thread | None = None
        self._supervisor_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._wake = threading.Event()
        self._generation = 0
        self._failed_attempts = 0

    def start(self) -> None:
        self._supervisor_thread = threading.Thread(target=self._supervisor_loop, daemon=True)
        self._supervisor_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._close_socket()

    def _supervisor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                latest = tuple(self._get_token_ids())
            except Exception as exc:
                logger.warning(f"Active-token-id lookup failed: {exc!r}")
                self._sleep_until_wake(self._poll_interval)
                continue

            rolled = latest != self._current_token_ids
            disconnected = not self._connected.is_set()
            if rolled or disconnected:
                if self._failed_attempts:
                    delay = min(
                        RECONNECT_BASE_DELAY_S * (2 ** (self._failed_attempts - 1)),
                        RECONNECT_MAX_DELAY_S,
                    )
                    logger.info(f"Reconnect back-off {delay:.1f}s (attempt {self._failed_attempts})")
                    if self._sleep_until_wake(delay, wake_on_signal=False):
                        continue
                if self._current_token_ids is None:
                    reason = "initial_subscribe"
                elif rolled:
                    reason = "cycle_rolled"
                else:
                    reason = "connection_closed"
                logger.info(
                    f"Reopening CLOB market WS ({reason}) "
                    f"old={_mask(self._current_token_ids)} new={_mask(latest)}"
                )
                self._failed_attempts += 1
                self._reopen(latest, reason)

            self._sleep_until_wake(self._poll_interval)

    def _sleep_until_wake(self, timeout: float, *, wake_on_signal: bool = True) -> bool:
        """Sleep up to `timeout`, returning True if woken early by a disconnect."""
        if not wake_on_signal:
            return self._stop.wait(timeout)
        woken = self._wake.wait(timeout)
        self._wake.clear()
        return woken and not self._stop.is_set()

    def _close_socket(self) -> None:
        ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            ws.close()
        except Exception as exc:
            logger.warning(f"Closing CLOB market WS raised: {exc!r}")
        thread, self._ws_thread = self._ws_thread, None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)

    def _reopen(self, token_ids: tuple[str, ...], reason: str) -> None:
        # Bump the generation before closing: a socket we close on purpose must
        # not report itself as an unexpected disconnect and open a false gap.
        self._generation += 1
        generation = self._generation
        self._close_socket()
        self._connected.clear()
        self._current_token_ids = token_ids
        self._emit_connection_state(
            "connecting", {"generation": generation, "token_ids": list(token_ids), "reason": reason}
        )

        def _on_open(ws: WebSocketApp) -> None:
            ws.send(json.dumps({
                "assets_ids": list(token_ids),
                "type": "market",
                "custom_feature_enabled": True,
            }))
            self._connected.set()
            self._failed_attempts = 0
            self._emit_connection_state("subscribed", {
                "generation": generation,
                "token_ids": list(token_ids),
                "reason": reason,
            })
            logger.info(f"CLOB market channel subscribed: {_mask(token_ids)}")

        def _on_message(_ws: WebSocketApp, raw: str) -> None:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"Discarding non-JSON market-channel frame ({len(raw)} bytes)")
                return
            for event in msg if isinstance(msg, list) else [msg]:
                if not isinstance(event, dict):
                    continue
                try:
                    self._on_update(event)
                except Exception as exc:
                    logger.exception(f"on_book_update callback raised: {exc!r}")

        def _on_error(_ws: WebSocketApp, err: Exception) -> None:
            logger.warning(f"CLOB market WS error: {err!r}")

        def _on_close(_ws: WebSocketApp, status_code: int | None, message: str | None) -> None:
            if generation != self._generation:
                return
            self._connected.clear()
            self._emit_connection_state("disconnected", {
                "generation": generation,
                "status_code": status_code,
                "message": message or "",
            })
            if not self._stop.is_set():
                logger.warning(
                    f"CLOB market WS closed (code={status_code}) — supervisor reconnecting"
                )
                self._wake.set()

        ws = WebSocketApp(
            MARKET_WS_URL,
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )
        self._ws = ws

        def _run() -> None:
            ws.run_forever(ping_interval=10, ping_timeout=5)

        self._ws_thread = threading.Thread(target=_run, daemon=True)
        self._ws_thread.start()

    def wait_until_subscribed(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def _emit_connection_state(self, state: str, details: dict) -> None:
        if self._on_connection_state is None:
            return
        try:
            self._on_connection_state(state, details)
        except Exception as exc:
            logger.exception(f"on_connection_state callback raised: {exc!r}")


def _mask(token_ids: Sequence[str] | None) -> str:
    if not token_ids:
        return "none"
    return ", ".join(f"{token_id[:10]}…" for token_id in token_ids)
