"""Crypto engine bridge — the operator's crypto_backtest tool over a
versioned JSON contract.

The tool (private quant-analysis project, P1a engine: signal/execution
separation with SL/TP/trailing risk controls, parameter grid, and
sortino/calmar/exposure metrics) is invoked as a subprocess without a
shell: the configured command receives ``--symbol --timeframe --start
--end --strategy --json`` appended and must print exactly one
``crypto.backtest.v1`` JSON object on stdout. Everything fails closed:
no command configured, a non-zero exit, unparsable stdout, a schema
mismatch, or missing fields is an error the screen renders verbatim —
never a best-effort guess.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any

SCHEMA = "crypto.backtest.v1"
DEFAULT_TIMEOUT = 900.0

_REQUIRED_NUMERIC = (
    "total_return_pct", "max_drawdown_pct", "sharpe_ratio", "sortino_ratio",
    "calmar_ratio", "exposure_pct", "win_rate", "total_trades",
    "profit_factor",
)


class CryptoBacktestError(RuntimeError):
    """The engine bridge failed (config, transport, or contract)."""


def build_args(config: Any, *, symbol: str, timeframe: str, start: str,
               end: str | None = None, strategy: str = "all") -> list[str]:
    """Tokenize the operator command and append the contract arguments."""
    raw = str(config.get("crypto_backtest_cmd", "") or "").strip()
    if not raw:
        raise CryptoBacktestError(
            "crypto_backtest_cmd is not configured — set it with: "
            "stammtisch config set crypto_backtest_cmd '<command>'")
    args = shlex.split(raw)
    if not args:
        raise CryptoBacktestError("crypto_backtest_cmd is empty after tokenizing")
    args += ["--symbol", symbol, "--timeframe", timeframe, "--start", start,
             "--strategy", strategy, "--json"]
    if end:
        args += ["--end", end]
    return args


def validate_payload(payload: Any) -> dict[str, Any]:
    """Fail-closed contract check; returns the payload on success."""
    if not isinstance(payload, dict):
        raise CryptoBacktestError("engine output is not a JSON object")
    if payload.get("schema") != SCHEMA:
        raise CryptoBacktestError(
            f"engine output schema mismatch: {payload.get('schema')!r} "
            f"(expected {SCHEMA!r})")
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        raise CryptoBacktestError("engine payload carries no results")
    for index, row in enumerate(results):
        if not isinstance(row, dict) or not row.get("strategy"):
            raise CryptoBacktestError(
                f"engine result #{index} has no strategy name")
        for field in _REQUIRED_NUMERIC:
            value = row.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise CryptoBacktestError(
                    f"engine result {row['strategy']!r} field {field!r} "
                    f"is not numeric: {value!r}")
    return payload


def run(config: Any, *, symbol: str, timeframe: str, start: str,
        end: str | None = None, strategy: str = "all",
        timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """One engine invocation; raises CryptoBacktestError on any failure."""
    args = build_args(config, symbol=symbol, timeframe=timeframe, start=start,
                      end=end, strategy=strategy)
    # The engine subprocess imports the same quantkit tree the TUI uses
    # (quantkit_path): the evolved tree's ccxt fetcher honors proxy env
    # vars, which the public one does not.
    env = dict(os.environ)
    quantkit_path = str(config.get("quantkit_path", "") or "").strip()
    if quantkit_path:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (quantkit_path + ":" + existing) if existing else quantkit_path
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout,
                              env=env)
    except FileNotFoundError as exc:
        raise CryptoBacktestError(f"engine command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CryptoBacktestError(
            f"engine timed out after {timeout:.0f}s") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace")[:300]
        raise CryptoBacktestError(
            f"engine exited {proc.returncode}: {stderr or 'no stderr'}")
    try:
        payload = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except ValueError as exc:
        stdout = proc.stdout.decode(errors="replace")[:300]
        raise CryptoBacktestError(
            f"engine stdout is not JSON: {exc}: {stdout!r}") from exc
    return validate_payload(payload)
