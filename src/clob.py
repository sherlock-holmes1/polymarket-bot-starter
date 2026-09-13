"""Authenticated CLOB client initialization.

Builds a `py_clob_client_v2.ClobClient` configured with the wallet, proxy address,
and L2 API credentials from environment variables.
"""
from __future__ import annotations

import os

from py_clob_client_v2 import ClobClient
from py_clob_client_v2.clob_types import ApiCreds

from src.utils import get_logger

logger = get_logger(__name__)

CLOB_BASE_URL = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


def build_client() -> ClobClient:
    """Return a fully-authenticated ClobClient for the bot's configured wallet."""
    private_key = os.environ["POLY_PRIVATE_KEY"]
    proxy_address = os.environ["POLY_PROXY_ADDRESS"]
    sig_type = int(os.environ.get("POLY_SIGNATURE_TYPE", "1"))

    creds = ApiCreds(
        api_key=os.environ["POLY_API_KEY"],
        api_secret=os.environ["POLY_API_SECRET"],
        api_passphrase=os.environ["POLY_API_PASSPHRASE"],
    )

    client = ClobClient(
        host=CLOB_BASE_URL,
        key=private_key,
        chain_id=POLYGON_CHAIN_ID,
        creds=creds,
        signature_type=sig_type,
        funder=proxy_address,
    )
    logger.info(f"ClobClient initialized for proxy {proxy_address[:8]}…")
    return client
