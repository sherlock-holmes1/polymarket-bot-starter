"""Logging + retry helpers shared across modules."""
from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

T = TypeVar("T")

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    attempts: int = 4,
    base_delay: float = 1.0,
    factor: float = 2.0,
    logger: logging.Logger | None = None,
) -> T:
    """Call `fn` with exponential back-off on exception. Re-raises the last exception."""
    log = logger or get_logger(__name__)
    delay = base_delay
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt == attempts:
                break
            log.warning(
                f"Attempt {attempt}/{attempts} failed: {exc!r}; retrying in {delay:.1f}s"
            )
            time.sleep(delay)
            delay *= factor
    assert last_exc is not None
    raise last_exc
