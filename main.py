"""Polymarket BTC Up/Down 15M trading bot — entry point.

This file wires the modules together. Each module is filled in across the
eight-step tutorial. Running it as-is prints a banner and exits.
"""
from __future__ import annotations

from dotenv import load_dotenv

from src.utils import get_logger

load_dotenv()
logger = get_logger(__name__)


def main() -> None:
    logger.info("=" * 60)
    logger.info("Polymarket BTC Up/Down 15M Bot — starter skeleton")
    logger.info("Follow the tutorial steps to fill in each module.")
    logger.info("Reference docs are in ./docs/")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
