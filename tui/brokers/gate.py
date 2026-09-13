"""The trading gate — a single explicit switch, fail-closed.

``trading_mode`` in the operator config is the only thing that turns on
order placement, and even then only against the pinned sandbox
endpoints. Empty means disabled: read-only screens still render, every
mutating call raises :class:`BrokerRefused` before touching the wire.
"""

from __future__ import annotations

from typing import Any

from .errors import BrokerRefused

MODE_PAPER = "paper"


def trading_mode(config: Any) -> str:
    try:
        mode = str(config.get("trading_mode", "") or "").strip().lower()
    except AttributeError:
        mode = str(getattr(config, "trading_mode", "") or "").strip().lower()
    return mode if mode == MODE_PAPER else ""


def ensure_trading_allowed(config: Any, action: str) -> None:
    if trading_mode(config) != MODE_PAPER:
        raise BrokerRefused(
            f"{action} refused: trading_mode is not '{MODE_PAPER}' "
            "(set it with: stammtisch config set trading_mode paper)")
