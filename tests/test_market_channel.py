"""Reconnect behaviour of the CLOB market-channel supervisor.

The milestone requires a reconnect when the stream closes *even if the token IDs
have not changed*, and a fresh snapshot request on the way back. These tests drive
the supervisor against a fake WebSocketApp so the close path is exercised without
a network.
"""
from __future__ import annotations

import json
import threading

import pytest

from src import market_channel
from src.market_channel import MarketChannelSupervisor

UP, DOWN = "up-token", "down-token"


class FakeWebSocketApp:
    """Stands in for `websocket.WebSocketApp`; `drop()` simulates a server close."""

    created: list["FakeWebSocketApp"] = []
    opened = threading.Semaphore(0)

    def __init__(self, url, on_open, on_message, on_error, on_close) -> None:
        self.url = url
        self._on_open = on_open
        self.on_message = on_message
        self._on_close = on_close
        self.sent: list[str] = []
        self.close_code: int | None = None
        self._released = threading.Event()
        FakeWebSocketApp.created.append(self)

    def send(self, data: str) -> None:
        self.sent.append(data)

    def run_forever(self, **_kwargs: object) -> None:
        self._on_open(self)
        FakeWebSocketApp.opened.release()
        self._released.wait(5)
        self._on_close(self, self.close_code, "")

    def close(self) -> None:
        self.close_code = 1000
        self._released.set()

    def drop(self) -> None:
        """Server-side close: the supervisor did not ask for this."""
        self.close_code = 1006
        self._released.set()

    @property
    def subscribed_assets(self) -> list[str]:
        return json.loads(self.sent[0])["assets_ids"] if self.sent else []


@pytest.fixture
def fake_ws(monkeypatch: pytest.MonkeyPatch) -> type[FakeWebSocketApp]:
    FakeWebSocketApp.created = []
    FakeWebSocketApp.opened = threading.Semaphore(0)
    monkeypatch.setattr(market_channel, "WebSocketApp", FakeWebSocketApp)
    return FakeWebSocketApp


def _wait_for_open(fake_ws: type[FakeWebSocketApp], timeout: float = 5.0) -> None:
    assert fake_ws.opened.acquire(timeout=timeout), "socket never opened"


def test_it_subscribes_to_the_selected_tokens_on_first_connect(fake_ws) -> None:
    states: list[tuple[str, dict]] = []
    supervisor = MarketChannelSupervisor(
        lambda: [UP, DOWN], lambda _event: None, poll_interval_s=60,
        on_connection_state=lambda state, details: states.append((state, details)),
    )
    supervisor.start()
    try:
        _wait_for_open(fake_ws)
        assert supervisor.wait_until_subscribed(5)
        assert fake_ws.created[0].subscribed_assets == [UP, DOWN]
        assert json.loads(fake_ws.created[0].sent[0])["custom_feature_enabled"] is True
        assert [state for state, _ in states] == ["connecting", "subscribed"]
        assert states[0][1]["reason"] == "initial_subscribe"
    finally:
        supervisor.stop()


def test_it_reconnects_after_a_server_close_without_a_token_change(fake_ws) -> None:
    states: list[tuple[str, dict]] = []
    supervisor = MarketChannelSupervisor(
        lambda: [UP, DOWN], lambda _event: None, poll_interval_s=60,
        on_connection_state=lambda state, details: states.append((state, details)),
    )
    supervisor.start()
    try:
        _wait_for_open(fake_ws)
        assert supervisor.wait_until_subscribed(5)

        fake_ws.created[0].drop()
        # A 60s poll interval must not delay this — the close wakes the supervisor.
        _wait_for_open(fake_ws, timeout=10)
        assert supervisor.wait_until_subscribed(5)

        assert len(fake_ws.created) == 2
        assert fake_ws.created[1].subscribed_assets == [UP, DOWN]
        reported = [state for state, _ in states]
        assert reported == ["connecting", "subscribed", "disconnected", "connecting", "subscribed"]
        assert states[2][1]["status_code"] == 1006
        assert states[3][1]["reason"] == "connection_closed"
    finally:
        supervisor.stop()


def test_a_token_roll_reopens_without_reporting_a_false_disconnect(fake_ws) -> None:
    """Closing a socket on purpose is not a data gap — only a server close is."""
    states: list[tuple[str, dict]] = []
    tokens = [[UP, DOWN]]
    supervisor = MarketChannelSupervisor(
        lambda: tokens[0], lambda _event: None, poll_interval_s=1,
        on_connection_state=lambda state, details: states.append((state, details)),
    )
    supervisor.start()
    try:
        _wait_for_open(fake_ws)
        assert supervisor.wait_until_subscribed(5)
        tokens[0] = ["next-up", "next-down"]
        _wait_for_open(fake_ws, timeout=10)
        assert supervisor.wait_until_subscribed(5)

        assert fake_ws.created[1].subscribed_assets == ["next-up", "next-down"]
        assert "disconnected" not in [state for state, _ in states]
        assert states[2][1]["reason"] == "cycle_rolled"
    finally:
        supervisor.stop()


def test_frames_are_unpacked_per_event_and_bad_frames_are_dropped(fake_ws) -> None:
    """The venue batches events into a JSON array; each one reaches the consumer."""
    received: list[dict] = []
    supervisor = MarketChannelSupervisor(lambda: [UP], received.append, poll_interval_s=60)
    supervisor.start()
    try:
        _wait_for_open(fake_ws)
        socket = fake_ws.created[0]
        socket.on_message(socket, json.dumps({"event_type": "book", "asset_id": UP}))
        socket.on_message(socket, json.dumps([
            {"event_type": "price_change", "price_changes": []},
            {"event_type": "last_trade_price", "asset_id": UP},
        ]))
        socket.on_message(socket, "not json at all")
        socket.on_message(socket, json.dumps(["a bare string is not an event"]))

        assert [event["event_type"] for event in received] == [
            "book", "price_change", "last_trade_price",
        ]
    finally:
        supervisor.stop()


def test_a_raising_consumer_does_not_kill_the_stream(fake_ws) -> None:
    seen: list[dict] = []

    def explode(event: dict) -> None:
        seen.append(event)
        raise ValueError("consumer blew up")

    supervisor = MarketChannelSupervisor(lambda: [UP], explode, poll_interval_s=60)
    supervisor.start()
    try:
        _wait_for_open(fake_ws)
        socket = fake_ws.created[0]
        socket.on_message(socket, json.dumps({"event_type": "book", "asset_id": UP}))
        socket.on_message(socket, json.dumps({"event_type": "book", "asset_id": UP}))
        assert len(seen) == 2
    finally:
        supervisor.stop()
