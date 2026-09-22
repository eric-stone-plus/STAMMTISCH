"""Timeseries forecast driver — subprocess + JSON contract.

The TUI shells out to a configured forecast command (any time-series
model — Kronos, Chronos, a local script, ...). The contract is
deliberately tiny so model runtimes live outside the TUI process:

    {cmd} SYMBOL --horizon N --json

prints exactly one JSON object on stdout. Legacy adapters may return:

    {"model": "...", "forecast": [float, ...], "dates": ["YYYY-MM-DD", ...]}

`dates` is optional and must have the same length as `forecast`. Legacy
responses are always marked ABSTAIN. A `kronos.forecast.v2` response must
also carry its sealed request, runtime, digest, and decision gate. No command
configured (`kronos_cmd` empty) degrades to {"ok": False}.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
import re
import shlex
from typing import Any

from .subproc import run_bounded


_SCHEDULE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_STALE_OHLCV_RE = re.compile(
    r"latest OHLCV bar is stale: expected (\d{4}-\d{2}-\d{2}), "
    r"got (\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def _forecast_error(detail: str) -> str:
    """Keep overlay failures bounded without hiding the freshness gate."""
    text = str(detail or "").strip()
    stale = _STALE_OHLCV_RE.search(text)
    if stale:
        expected, observed = stale.groups()
        return (
            f"forecast unavailable: latest OHLCV bar is {observed}; "
            f"expected completed session {expected}"
        )
    return text[:200]


class TimeseriesDriver:
    """Thin wrapper around a forecast subprocess."""

    def __init__(self, cmd: str | None = None, timeout: int = 600):
        self.cmd = (cmd or "").strip()
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.cmd)

    def forecast(
        self,
        symbol: str,
        horizon: int = 20,
        *,
        validated_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one forecast; returns {"ok": bool, ...} exactly like the
        engine methods so callers share one error-handling shape."""
        if not self.available:
            return {"ok": False, "error": "no forecast command configured (set kronos_cmd)"}
        try:
            argv = shlex.split(self.cmd)
        except ValueError as e:
            return {"ok": False, "error": f"bad kronos_cmd: {e}"}

        out = run_bounded(
            argv + [symbol, "--horizon", str(horizon), "--json"],
            self.timeout,
            label="forecast command",
        )
        if not out["ok"]:
            return {"ok": False, "error": _forecast_error(out["error"])}

        raw = out["stdout"].strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"forecast output is not JSON: "
                                          f"{raw[:200] or out['stderr'].strip()[:200]}"}
        if out["returncode"] != 0:
            detail = ""
            if isinstance(data, dict):
                detail = str(data.get("error", ""))
            detail = detail or out["stderr"].strip() or f"exit code {out['returncode']}"
            return {"ok": False, "error": _forecast_error(
                f"forecast command failed: {detail[:200]}")}
        if not isinstance(data, dict) or data.get("ok") is False:
            detail = data.get("error", "forecast command returned ok=false") if isinstance(data, dict) else ""
            return {"ok": False, "error": _forecast_error(str(detail))}
        if "forecast" not in data:
            return {"ok": False, "error": "forecast JSON lacks a 'forecast' list"}
        forecast = data["forecast"]
        dates = data.get("dates", [])
        if not isinstance(forecast, list) or not all(
            type(value) in (int, float)
            and math.isfinite(value)
            and value > 0
            for value in forecast
        ):
            return {
                "ok": False,
                "error": "'forecast' must be a list of finite positive numbers",
            }
        if len(forecast) != horizon:
            return {
                "ok": False,
                "error": f"'forecast' length does not match horizon {horizon}",
            }
        if dates and len(dates) != len(forecast):
            return {"ok": False, "error": "'dates' length does not match 'forecast'"}

        v2 = data.get("schema") == "kronos.forecast.v2"
        if v2:
            error = self._validate_kronos_v2(data, symbol, horizon)
            if error:
                return {"ok": False, "error": error}
            if validated_context is not None:
                error = self._validate_consensus_link(data, validated_context)
                if error:
                    return {"ok": False, "error": error}
        else:
            if validated_context is not None:
                return {
                    "ok": False,
                    "error": "validated bars require a linkable Kronos v2 receipt",
                }
            if dates and not self._valid_dates(dates):
                return {"ok": False, "error": "'dates' must be unique ordered ISO dates"}
            # Legacy adapters have no decision receipt. They are parsed for
            # diagnostics but must never be silently promoted to evidence.
            data = {
                **data,
                "decision_gate": {
                    "status": "ABSTAIN",
                    "reasons": ["legacy forecast output lacks the Kronos v2 receipt"],
                },
            }

        provenance = {
            key: data[key]
            for key in (
                "schema",
                "forecast_digest",
                "cache_key",
                "artifact_hash",
                "request",
                "runtime",
                "decision_gate",
                "generated_at",
            )
            if key in data
        }
        if validated_context is not None:
            provenance["validated_bars_reference"] = validated_context["reference"]
        gate = data["decision_gate"]
        if gate.get("status") != "PASS":
            reasons = gate.get("reasons")
            reason = "; ".join(reasons) if isinstance(reasons, list) else ""
            suffix = f": {reason}" if reason else ""
            return {
                "ok": False,
                "execution_ok": True,
                "error": f"forecast decision gate {gate.get('status', 'invalid')}{suffix}",
                "model": data.get("model", "?"),
                # Diagnostics, not evidence: the chart may draw these values,
                # always labeled with the non-PASS gate status.
                "forecast": [float(v) for v in forecast],
                "dates": list(dates),
                "provenance": provenance,
            }

        return {
            "ok": True,
            "model": data.get("model", "?"),
            "forecast": [float(v) for v in forecast],
            "dates": list(dates),
            "provenance": provenance,
        }

    @staticmethod
    def _validate_consensus_link(
        data: dict[str, Any], context: dict[str, Any]
    ) -> str | None:
        """Prove the forecast snapshot is an exact tail of verified daily bars."""

        if not isinstance(context, dict) or set(context) != {
            "reference",
            "bars",
            "calendar_sessions",
        }:
            return "validated bars context has an invalid shape"
        reference = context.get("reference")
        bars = context.get("bars")
        sessions = context.get("calendar_sessions")
        if not isinstance(reference, dict) or not isinstance(bars, list) or not bars:
            return "validated bars context is incomplete"
        if not isinstance(sessions, list) or not sessions:
            return "validated bars calendar sessions are unavailable"
        identity = reference.get("identity")
        request = data.get("request")
        if not isinstance(identity, dict) or not isinstance(request, dict):
            return "validated bars identity cannot be linked"
        if request.get("symbol") != identity.get("symbol"):
            return "forecast symbol differs from the validated bars identity"
        if request.get("calendar") != identity.get("calendar"):
            return "forecast calendar differs from the validated bars identity"
        snapshot = request.get("input_snapshot")
        rows = snapshot.get("rows") if isinstance(snapshot, dict) else None
        if not isinstance(rows, list) or not rows or len(rows) > len(bars):
            return "forecast input is not a bounded tail of validated bars"
        tail = bars[-len(rows) :]
        if [bar.get("date") for bar in tail] != [
            str(row.get("timestamp", ""))[:10] for row in rows
        ]:
            return "forecast input sessions differ from the validated bars tail"
        if any(bar.get("date") not in sessions for bar in tail):
            return "forecast input contains a session outside the validated calendar"
        columns = snapshot.get("columns")
        expected_columns = ["open", "high", "low", "close", "volume"]
        if columns != expected_columns:
            return "forecast input columns differ from validated bars"
        for position, (bar, row) in enumerate(zip(tail, rows)):
            try:
                observed = [float.fromhex(value) for value in row["values_hex"]]
                expected = [float(bar[column]) for column in expected_columns]
            except (KeyError, TypeError, ValueError):
                return "forecast input cannot be compared with validated bars"
            if any(left.hex() != right.hex() for left, right in zip(observed, expected)):
                return (
                    "forecast input values differ from validated bars "
                    f"at tail position {position}"
                )
        return None

    @staticmethod
    def _valid_dates(values: Any) -> bool:
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, str) for value in values)
        ):
            return False
        try:
            parsed = [date.fromisoformat(value) for value in values]
        except ValueError:
            return False
        return parsed == sorted(set(parsed))

    @staticmethod
    def _validate_input_snapshot(request: dict[str, Any]) -> str | None:
        """Replay the embedded OHLCV snapshot and its content hash.

        The outer artifact hash only proves internal byte consistency.  This
        check binds the claimed input hash, row count, time bounds, and candle
        geometry to the exact model input instead of trusting those claims.
        """
        snapshot = request.get("input_snapshot")
        if not isinstance(snapshot, dict) or set(snapshot) != {
            "encoding",
            "columns",
            "rows",
        }:
            return "Kronos v2 input snapshot has an invalid shape"
        if snapshot.get("encoding") != "float.hex.v1":
            return "Kronos v2 input snapshot has an unsupported encoding"
        columns = snapshot.get("columns")
        rows = snapshot.get("rows")
        if columns != ["open", "high", "low", "close", "volume"]:
            return "Kronos v2 input snapshot has invalid OHLCV columns"
        if not isinstance(rows, list) or not rows:
            return "Kronos v2 input snapshot must contain rows"
        if type(request.get("input_rows")) is not int or request["input_rows"] != len(
            rows
        ):
            return "Kronos v2 input row count does not match the snapshot"

        digest = hashlib.sha256(b"kronos-ohlcv-v1\0")
        parsed_times: list[datetime] = []
        session_days: list[date] = []
        timestamp_texts: list[str] = []
        awareness: bool | None = None
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"timestamp", "values_hex"}:
                return "Kronos v2 input snapshot row has an invalid shape"
            timestamp = row.get("timestamp")
            values_hex = row.get("values_hex")
            if not isinstance(timestamp, str) or not isinstance(values_hex, list):
                return "Kronos v2 input snapshot row has invalid fields"
            if len(values_hex) != len(columns) or not all(
                isinstance(value, str) for value in values_hex
            ):
                return "Kronos v2 input snapshot row has invalid values"
            try:
                parsed_time = datetime.fromisoformat(timestamp)
                values = [float.fromhex(value) for value in values_hex]
            except ValueError:
                return "Kronos v2 input snapshot contains malformed values"
            if parsed_time.isoformat() != timestamp:
                return "Kronos v2 input snapshot timestamp is not canonical ISO"
            row_aware = parsed_time.tzinfo is not None
            if awareness is None:
                awareness = row_aware
            elif awareness != row_aware:
                return "Kronos v2 input snapshot mixes timezone conventions"
            if not all(math.isfinite(value) for value in values):
                return "Kronos v2 input snapshot contains non-finite values"
            if any(float(value).hex() != encoded for value, encoded in zip(values, values_hex)):
                return "Kronos v2 input snapshot values are not canonical float.hex"
            open_, high, low, close, volume = values
            if min(open_, high, low, close) <= 0 or volume < 0:
                return "Kronos v2 input snapshot contains invalid price or volume"
            if high < max(open_, low, close) or low > min(open_, high, close):
                return "Kronos v2 input snapshot contains inconsistent candle bounds"
            parsed_times.append(parsed_time)
            session_days.append(parsed_time.date())
            timestamp_texts.append(timestamp)
            digest.update("\x1f".join([timestamp, *values_hex]).encode("utf-8"))
            digest.update(b"\n")

        if parsed_times != sorted(parsed_times) or len(parsed_times) != len(
            set(parsed_times)
        ):
            return "Kronos v2 input snapshot timestamps must be unique and ordered"
        if len(session_days) != len(set(session_days)):
            return "Kronos v2 input snapshot contains multiple bars for one session"
        if request.get("input_start") != timestamp_texts[0]:
            return "Kronos v2 input_start does not match the snapshot"
        if request.get("input_end") != timestamp_texts[-1]:
            return "Kronos v2 input_end does not match the snapshot"
        if request.get("last_completed_session") != session_days[-1].isoformat():
            return "Kronos v2 last completed session does not match the snapshot"
        if request.get("input_hash") != digest.hexdigest():
            return "Kronos v2 input hash does not match the snapshot"
        return None

    @classmethod
    def _validate_kronos_v2(
        cls, data: dict[str, Any], symbol: str, horizon: int
    ) -> str | None:
        request = data.get("request")
        gate = data.get("decision_gate")
        runtime = data.get("runtime")
        if not isinstance(request, dict):
            return "Kronos v2 receipt lacks request provenance"
        required_request_fields = {
            "schema",
            "implementation_revision",
            "implementation_hash",
            "symbol",
            "requested_as_of",
            "decision_cutoff_utc",
            "input_end",
            "calendar",
            "last_completed_session",
            "forecast_dates",
            "input_hash",
            "input_rows",
            "input_start",
            "input_snapshot",
            "data",
            "runtime",
            "horizon",
            "model_id",
            "model_revision",
            "tokenizer_id",
            "tokenizer_revision",
            "inference",
            "calendar_schedule_sha256",
            "calendar_version",
            "forecast_session_opens_utc",
            "input_policy",
            "runtime_policy",
        }
        if set(request) != required_request_fields:
            return "Kronos v2 request provenance has an incomplete or unknown shape"
        if request.get("schema") != "kronos.forecast.v2":
            return "Kronos v2 request schema does not match the response"
        if request.get("symbol") != symbol.strip().upper():
            return "Kronos v2 receipt symbol does not match the request"
        if request.get("horizon") != horizon:
            return "Kronos v2 receipt horizon does not match the request"
        snapshot_error = cls._validate_input_snapshot(request)
        if snapshot_error:
            return snapshot_error
        if data.get("dates") != request.get("forecast_dates") or not cls._valid_dates(
            data.get("dates")
        ):
            return "Kronos v2 forecast dates do not match the sealed request"
        schedule_digest = request.get("calendar_schedule_sha256")
        if not isinstance(schedule_digest, str) or _SCHEDULE_DIGEST_RE.fullmatch(
            schedule_digest
        ) is None:
            return "Kronos v2 calendar schedule digest is invalid"
        calendar_version = request.get("calendar_version")
        if not isinstance(calendar_version, str) or not calendar_version.strip():
            return "Kronos v2 calendar version is missing"
        session_opens = request.get("forecast_session_opens_utc")
        if not isinstance(session_opens, list) or len(session_opens) != len(
            request["forecast_dates"]
        ):
            return "Kronos v2 forecast session opens do not match the forecast dates"
        for opening in session_opens:
            if not isinstance(opening, str):
                return "Kronos v2 forecast session opens must be ISO instants"
            try:
                parsed_opening = datetime.fromisoformat(opening)
            except ValueError:
                return "Kronos v2 forecast session opens must be ISO instants"
            if parsed_opening.tzinfo is None:
                return "Kronos v2 forecast session opens must include a timezone"
        input_policy = request.get("input_policy")
        if not isinstance(input_policy, dict):
            return "Kronos v2 input policy has an invalid shape"
        if input_policy.get("schema") != "kronos.input-policy.v1":
            return "Kronos v2 input policy schema is unsupported"
        if input_policy.get("columns") != ["open", "high", "low", "close", "volume"]:
            return "Kronos v2 input policy columns differ from the input snapshot"
        if input_policy.get("interval") != "1d":
            return "Kronos v2 input policy interval must be 1d"
        lookback = input_policy.get("lookback")
        if (
            type(lookback) is not int
            or lookback < 1
            or request["input_rows"] > lookback
        ):
            return "Kronos v2 input policy lookback does not cover the input rows"
        runtime_policy = request.get("runtime_policy")
        if not isinstance(runtime_policy, dict):
            return "Kronos v2 runtime policy has an invalid shape"
        if (
            runtime_policy.get("implementation_revision")
            != request["implementation_revision"]
            or runtime_policy.get("implementation_hash")
            != request["implementation_hash"]
        ):
            return "Kronos v2 runtime policy differs from the sealed implementation"
        if not isinstance(gate, dict):
            return "Kronos v2 receipt lacks a valid decision gate"
        if gate.get("status") == "PASS":
            return (
                "Kronos v2 cannot assert PASS: no independently validated "
                "production gate receipt is defined"
            )
        if gate.get("status") not in {"ABSTAIN", "FAIL"}:
            return "Kronos v2 receipt lacks a valid decision gate"
        if not isinstance(runtime, dict) or runtime != request.get("runtime"):
            return "Kronos v2 runtime identity does not match the sealed request"
        expected_digest = hashlib.sha256(
            json.dumps(
                [float(value) for value in data["forecast"]],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if data.get("forecast_digest") != expected_digest:
            return "Kronos v2 forecast digest does not match the values"
        canonical_request = json.dumps(
            request, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        expected_key = hashlib.sha256(canonical_request).hexdigest()
        if data.get("cache_key") != expected_key:
            return "Kronos v2 cache key does not match the sealed request"
        generated_at = data.get("generated_at")
        if not isinstance(generated_at, str):
            return "Kronos v2 receipt lacks generated_at"
        try:
            generated = datetime.fromisoformat(generated_at)
        except ValueError:
            return "Kronos v2 generated_at is not an ISO datetime"
        if generated.tzinfo is None:
            return "Kronos v2 generated_at must include a timezone"
        artifact_hash = data.get("artifact_hash")
        if not isinstance(artifact_hash, str):
            return "Kronos v2 receipt lacks artifact_hash"
        artifact = {key: value for key, value in data.items() if key != "artifact_hash"}
        expected_artifact_hash = "sha256:" + hashlib.sha256(
            json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if artifact_hash != expected_artifact_hash:
            return "Kronos v2 artifact hash does not match the receipt"
        return None
