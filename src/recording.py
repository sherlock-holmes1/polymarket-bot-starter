"""Append-only public market-data recordings.

Every row carries three clocks so replay can separate venue time from our time:
  * `exchange_timestamp_ms` — the venue's own `timestamp`, when it supplies one.
  * `received_unix_ms` / `received_at` — wall clock at local receipt.
  * `received_monotonic_ns` — monotonic clock at local receipt, immune to NTP steps.

Payloads are stored verbatim. Outcome labels, hashes, and any field this project
does not yet parse survive into the file untouched.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENTS_FILENAME = "events.jsonl"
METADATA_FILENAME = "metadata.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def unix_ms_to_iso(unix_ms: int) -> str:
    return (
        datetime.fromtimestamp(unix_ms / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


class JsonlRecorder:
    """Write metadata once and timestamped events in arrival order."""

    def __init__(self, directory: Path, metadata: dict[str, Any]) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=False)
        self._events = (directory / EVENTS_FILENAME).open("x", encoding="utf-8")
        self._lock = threading.Lock()
        self._sequence = 0
        self.write_metadata(metadata)

    @property
    def events_path(self) -> Path:
        return self.directory / EVENTS_FILENAME

    def write_metadata(self, metadata: dict[str, Any]) -> None:
        payload = {"recorded_at": utc_now_iso(), **_json_ready(metadata)}
        (self.directory / METADATA_FILENAME).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def record(self, kind: str, payload: dict[str, Any]) -> int:
        received = datetime.now(timezone.utc)
        ready = _json_ready(payload)
        exchange_ms = _exchange_timestamp_ms(ready)
        with self._lock:
            self._sequence += 1
            row = {
                "sequence": self._sequence,
                "received_at": received.isoformat().replace("+00:00", "Z"),
                "received_unix_ms": int(received.timestamp() * 1000),
                "received_monotonic_ns": time.monotonic_ns(),
                "exchange_timestamp_ms": exchange_ms,
                "kind": kind,
                "payload": ready,
            }
            self._events.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
            self._events.flush()
            return self._sequence

    def close(self) -> None:
        with self._lock:
            if not self._events.closed:
                self._events.close()


def _exchange_timestamp_ms(payload: Any) -> int | None:
    """Pull the venue timestamp out of a market event, or return None.

    Market-channel events carry `timestamp` as a millisecond epoch string. Some
    price-change payloads only carry it on the nested changes.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("timestamp")
    parsed = _parse_epoch_ms(raw)
    if parsed is not None:
        return parsed
    for change in payload.get("price_changes") or []:
        if isinstance(change, dict):
            parsed = _parse_epoch_ms(change.get("timestamp"))
            if parsed is not None:
                return parsed
    return None


def _parse_epoch_ms(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _json_ready(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_ready(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value
