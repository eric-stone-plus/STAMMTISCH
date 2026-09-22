"""Broker execution layer — Alpaca paper + Binance spot testnet ONLY.

The sandbox rule is structural, not a default: the Alpaca base URL is
pinned to ``paper-api.alpaca.markets`` and the Binance base to
``testnet.binance.vision``; mainnet endpoints are refused in code
unconditionally, mirroring the GALAHAD ``nautilus_live`` testnet gate.
On top of that, every mutating call passes the ``trading_mode`` gate:
with the config key empty the brokers render account state read-only
and refuse to place or cancel orders.

Credentials resolve from the environment first (ALPACA_PAPER_API_KEY /
BINANCE_TESTNET_API_KEY naming), then from operator-declared .env files
(config ``alpaca_env_file`` / ``binance_env_file``) — host-specific
paths live only in the operator's own config, never in the repo.
"""

from __future__ import annotations

from .errors import BrokerError, BrokerRefused
from .gate import ensure_trading_allowed, trading_mode
from .alpaca import AlpacaBroker
from .binance import BinanceBroker

__all__ = [
    "AlpacaBroker",
    "BinanceBroker",
    "BrokerError",
    "BrokerRefused",
    "ensure_trading_allowed",
    "trading_mode",
]
