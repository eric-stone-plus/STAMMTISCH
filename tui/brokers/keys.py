"""Credential resolution for the sandbox brokers.

Precedence: environment variables first (the naming the GALAHAD
products already use), then the operator-declared .env file from the
TUI config. Values are never logged or echoed; a missing credential is
a clean ``BrokerRefused`` at call time, never a guessed default.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import BrokerRefused

_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse_env_file(path: str | Path) -> dict[str, str]:
    """Parse a dotenv-style file into a dict (comments and quotes handled)."""
    values: dict[str, str] = {}
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1]
        if key and raw:
            values[key] = raw
    return values


def _resolve(config: Any, env_names: tuple[str, ...], secret_names: tuple[str, ...],
             env_file_key: str) -> tuple[str, str]:
    def _lookup(names: tuple[str, ...]) -> str:
        for name in names:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return ""

    key, secret = _lookup(env_names), _lookup(secret_names)
    if key and secret:
        return key, secret
    file_path = ""
    if config is not None:
        file_path = str(config.get(env_file_key, "") or "").strip()
    if file_path:
        stored = parse_env_file(file_path)
        key = key or next((stored[name] for name in env_names if stored.get(name)), "")
        secret = secret or next((stored[name] for name in secret_names if stored.get(name)), "")
    if not key or not secret:
        raise BrokerRefused(
            f"credentials missing for {env_file_key} (env {env_names[0]}… "
            f"or config {env_file_key})")
    return key, secret


@dataclass(frozen=True)
class Credentials:
    key: str
    secret: str


def alpaca_credentials(config: Any) -> Credentials:
    key, secret = _resolve(
        config,
        ("ALPACA_PAPER_API_KEY", "APCA_API_KEY_ID", "ALPACA_API_KEY"),
        ("ALPACA_PAPER_API_SECRET", "APCA_API_SECRET_KEY", "ALPACA_API_SECRET"),
        "alpaca_env_file")
    return Credentials(key=key, secret=secret)


def binance_credentials(config: Any) -> Credentials:
    key, secret = _resolve(
        config,
        ("BINANCE_TESTNET_API_KEY", "BINANCE_API_KEY"),
        ("BINANCE_TESTNET_API_SECRET", "BINANCE_API_SECRET"),
        "binance_env_file")
    return Credentials(key=key, secret=secret)
