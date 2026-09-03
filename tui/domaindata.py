"""Domain board driver — subprocess + JSON contract.

The TUI shells out to a configured domain command (e.g. an exchange daily
settlement adapter living outside this repo). The contract is deliberately
tiny so data runtimes live outside the TUI process:

    {cmd}

prints exactly one JSON object on stdout:

    {"ok": true, "schema": "mktdaily.sgx-board.v1", "asof": "YYYY-MM-DD",
     "source": "...", "instruments": [ ... ]}

No command configured, a spawn failure, a nonzero exit, or malformed JSON
all degrade to {"ok": False, "error": ...} — the screen renders the error,
never a stale or partial board.
"""

from __future__ import annotations

import json
import math
from typing import Any

from .subproc import run_bounded

BOARD_SCHEMA = "mktdaily.sgx-board.v1"
SPVAL_SCHEMA = "stammtisch.spval-board.v1"
SPVAL_SCHEMA_V2 = "stammtisch.spval-board.v2"


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def validate_board(payload: Any) -> str | None:
    """Shape-check one board payload; returns an error string or None.

    Strict on the fields the screen renders; extra keys pass through so the
    adapter can grow without a TUI release.
    """
    if not isinstance(payload, dict):
        return "board payload is not a JSON object"
    if payload.get("ok") is not True:
        return "board payload does not assert ok"
    if payload.get("schema") != BOARD_SCHEMA:
        return f"board schema is not {BOARD_SCHEMA}"
    asof = payload.get("asof")
    if not isinstance(asof, str) or len(asof) != 10:
        return "board asof must be an ISO date"
    instruments = payload.get("instruments")
    if not isinstance(instruments, list):
        return "board instruments must be a list"
    for instrument in instruments:
        if not isinstance(instrument, dict):
            return "board instrument entries must be objects"
        for key in ("code", "name", "unit", "group", "front_month"):
            if not isinstance(instrument.get(key), str) or not instrument[key]:
                return f"board instrument {key} must be a non-empty string"
        if not _finite_number(instrument.get("settle")):
            return f"board instrument {instrument.get('code', '?')} settle must be finite"
        for key in ("change", "change_pct"):
            value = instrument.get(key)
            if value is not None and not _finite_number(value):
                return f"board instrument {instrument.get('code', '?')} {key} must be finite or null"
        curve = instrument.get("curve")
        if not isinstance(curve, list) or any(
            not isinstance(point, dict)
            or not isinstance(point.get("month"), str)
            or not _finite_number(point.get("settle"))
            for point in curve
        ):
            return f"board instrument {instrument.get('code', '?')} curve is malformed"
        recent = instrument.get("recent")
        if not isinstance(recent, list) or any(
            not isinstance(point, dict)
            or not isinstance(point.get("date"), str)
            or not _finite_number(point.get("settle"))
            for point in recent
        ):
            return f"board instrument {instrument.get('code', '?')} recent is malformed"
    return None


def validate_spval_board(payload: Any) -> str | None:
    """Shape-check one S&P valuation board payload (stammtisch.spval-board.v1).

    Strict on the fields the screen renders; extra keys pass through so the
    adapter can grow without a TUI release.
    """
    if not isinstance(payload, dict):
        return "spval payload is not a JSON object"
    if payload.get("ok") is not True:
        return "spval payload does not assert ok"
    if payload.get("schema") != SPVAL_SCHEMA:
        return f"spval schema is not {SPVAL_SCHEMA}"
    asof = payload.get("asof")
    if not isinstance(asof, str) or len(asof) != 10:
        return "spval asof must be an ISO date"
    kpis = payload.get("kpis")
    if not isinstance(kpis, dict):
        return "spval kpis must be an object"
    for key in ("ret_med", "ret_p5", "ret_p95", "p_loss", "es10",
                "irr_med_pct", "payback_med_yr", "ebitda_y1_med"):
        if not _finite_number(kpis.get(key)):
            return f"spval kpis.{key} must be finite"
    grid = payload.get("grid")
    if not isinstance(grid, list) or not grid:
        return "spval grid must be a non-empty list"
    for row in grid:
        if not isinstance(row, dict):
            return "spval grid entries must be objects"
        if not isinstance(row.get("key"), str) or not row["key"]:
            return "spval grid key must be a non-empty string"
        for key in ("price", "ret_med", "ret_p5", "p_loss", "es10",
                    "irr_med_pct"):
            if not _finite_number(row.get(key)):
                return f"spval grid {row.get('key', '?')}.{key} must be finite"
    maxbid = payload.get("maxbid")
    if not isinstance(maxbid, dict):
        return "spval maxbid must be an object"
    for key in ("m1", "m2", "m3", "m4", "m5", "pim", "base", "d1", "d2",
                "value"):
        if not _finite_number(maxbid.get(key)):
            return f"spval maxbid.{key} must be finite"
    greeks = payload.get("greeks")
    if not isinstance(greeks, list):
        return "spval greeks must be a list"
    for entry in greeks:
        if not isinstance(entry, dict) or not isinstance(entry.get("factor"), str):
            return "spval greeks entries must be objects with a factor"
        if not _finite_number(entry.get("down")) or not _finite_number(entry.get("up")):
            return f"spval greeks {entry.get('factor', '?')} down/up must be finite"
    return None


def validate_spval_board_v2(payload: Any) -> str | None:
    """Shape-check one v2 board (stammtisch.spval-board.v2).

    v2 = the v1 S&P sections plus ``market`` (cycle/route/stats) and
    ``risk`` (tail matrix, counterfactuals, sensitivity) so one adapter
    payload feeds all SHIPPING categories. Extra keys pass through.
    """
    if not isinstance(payload, dict):
        return "spval payload is not a JSON object"
    if payload.get("ok") is not True:
        return "spval payload does not assert ok"
    if payload.get("schema") != SPVAL_SCHEMA_V2:
        return f"spval schema is not {SPVAL_SCHEMA_V2}"
    probe = dict(payload)
    probe["schema"] = SPVAL_SCHEMA
    base_error = validate_spval_board(probe)
    if base_error:
        return base_error
    market = payload.get("market")
    if not isinstance(market, dict):
        return "spval market must be an object"
    for key in ("cycle_annual_tc", "route_last24m", "tc_stats",
                "scrap_premium", "vol_by_year", "ou"):
        if key not in market:
            return f"spval market.{key} missing"
    for key in ("cycle_annual_tc", "route_last24m"):
        series = market[key]
        if not isinstance(series, dict) or not series:
            return f"spval market.{key} must be a non-empty object"
        for label, value in series.items():
            if not _finite_number(value):
                return f"spval market.{key}[{label}] must be finite"
    risk = payload.get("risk")
    if not isinstance(risk, dict):
        return "spval risk must be an object"
    tail = risk.get("tail_matrix")
    if not isinstance(tail, list) or not tail:
        return "spval risk.tail_matrix must be a non-empty list"
    for row in tail:
        if not isinstance(row, dict) or not isinstance(row.get("key"), str):
            return "spval tail entries must be objects with a key"
        for key in ("ret_med", "ret_p5", "ret_p95", "p_loss", "es10",
                    "irr_med_pct"):
            if not _finite_number(row.get(key)):
                return f"spval tail {row.get('key', '?')}.{key} must be finite"
    counterfactual = risk.get("counterfactual")
    if not isinstance(counterfactual, dict) or not counterfactual:
        return "spval risk.counterfactual must be a non-empty object"
    for label, value in counterfactual.items():
        if not _finite_number(value):
            return f"spval counterfactual[{label}] must be finite"
    sens = risk.get("sens_matrix")
    if not isinstance(sens, dict) or not sens:
        return "spval risk.sens_matrix must be a non-empty object"
    for price, cols in sens.items():
        if not isinstance(cols, dict):
            return "spval sens rows must be objects"
        for tce, value in cols.items():
            if not _finite_number(value):
                return f"spval sens[{price}][{tce}] must be finite"
    return None


class DomainDriver:
    """Thin wrapper around a domain board subprocess."""

    def __init__(self, argv: tuple[str, ...] | list[str], timeout: int = 180,
                 validator: Any = None):
        self.argv = tuple(str(part) for part in argv if str(part))
        self.timeout = timeout
        self._validator = validator or validate_board

    @property
    def available(self) -> bool:
        return bool(self.argv)

    def board(self) -> dict[str, Any]:
        """Run one board fetch; {"ok": bool, ...} like the engine methods."""
        if not self.available:
            return {"ok": False, "error": "no domain command configured (set shipping_cmd)"}
        out = run_bounded(list(self.argv), self.timeout, label="domain command")
        if not out["ok"]:
            return {"ok": False, "error": out["error"]}

        raw = out["stdout"].strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"domain output is not JSON: "
                                          f"{raw[:200] or out['stderr'].strip()[:200]}"}
        if out["returncode"] != 0:
            detail = ""
            if isinstance(data, dict):
                detail = str(data.get("error", ""))
            detail = detail or out["stderr"].strip() or f"exit code {out['returncode']}"
            return {"ok": False, "error": f"domain command failed: {detail[:200]}"}
        if not isinstance(data, dict) or data.get("ok") is False:
            detail = data.get("error", "domain command returned ok=false") if isinstance(data, dict) else ""
            return {"ok": False, "error": str(detail)}
        error = self._validator(data)
        if error:
            return {"ok": False, "error": error}
        return data
