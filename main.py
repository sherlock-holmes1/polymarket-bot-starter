"""Polymarket BTC Up/Down 15M trading bot — entry point.

This file wires the modules together. The read-only market-data recorder runs
separately with `python -m src.collector`.
"""
from __future__ import annotations

from dotenv import load_dotenv

from src.utils import get_logger

load_dotenv()
logger = get_logger(__name__)


def main() -> None:
    logger.info("=" * 60)
    logger.info("Polymarket BTC Up/Down 15M Bot")
    logger.info("Read-only recorder: python -m src.collector --list-rewarded")
    logger.info("Deterministic replay: python -m src.replay <recording> --verify")
    logger.info("Reference docs are in ./docs/")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
