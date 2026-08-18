"""TimeseriesDriver tests — subprocess JSON contract, fake commands only."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest

from tui.timeseries import TimeseriesDriver


def _fake_cmd(body: str, exit_code: int = 0) -> str:
    """A shell script that prints `body` and exits as requested."""
    d = tempfile.mkdtemp(prefix="stammtisch-fake-forecast-")
    script = os.path.join(d, "fake-forecast")
    with open(script, "w") as f:
        f.write(
            "#!/bin/sh\necho '%s'\nexit %d\n"
            % (body.replace("'", r"'\''"), exit_code)
        )
    os.chmod(script, 0o755)
    return script


def _v2_payload(
    *,
    horizon: int = 2,
    gate: str = "ABSTAIN",
    forecast_digest: str | None = None,
) -> dict:
    forecast = [101.0 + position for position in range(horizon)]
    dates = ["2026-01-02", "2026-01-05"][:horizon]
    snapshot_rows = []
    input_digest = hashlib.sha256(b"kronos-ohlcv-v1\0")
    for timestamp, values in (
        ("2025-12-30T00:00:00", [100.0, 102.0, 99.0, 101.0, 1000.0]),
        ("2025-12-31T00:00:00", [101.0, 103.0, 100.0, 102.0, 1100.0]),
    ):
        values_hex = [float(value).hex() for value in values]
        snapshot_rows.append({"timestamp": timestamp, "values_hex": values_hex})
        input_digest.update("\x1f".join([timestamp, *values_hex]).encode())
        input_digest.update(b"\n")
    request = {
        "schema": "kronos.forecast.v2",
        "implementation_revision": "2",
        "implementation_hash": "a" * 64,
        "symbol": "AAPL",
        "requested_as_of": "2026-01-01T20:00:00Z",
        "decision_cutoff_utc": "2026-01-01T20:00:00+00:00",
        "input_end": "2025-12-31T00:00:00",
        "calendar": "XNYS",
        "last_completed_session": "2025-12-31",
        "horizon": horizon,
        "forecast_dates": dates,
        "input_hash": input_digest.hexdigest(),
        "input_rows": len(snapshot_rows),
        "input_start": "2025-12-30T00:00:00",
        "input_snapshot": {
            "encoding": "float.hex.v1",
            "columns": ["open", "high", "low", "close", "volume"],
            "rows": snapshot_rows,
        },
        "data": {
            "provider": "test",
            "provider_point_in_time": False,
        },
        "runtime": {"device": "cpu", "model_snapshot": "sha256:test"},
        "model_id": "test/model",
        "model_revision": "c" * 40,
        "tokenizer_id": "test/tokenizer",
        "tokenizer_revision": "d" * 40,
        "inference": {"seed": 1729},
        "calendar_schedule_sha256": "sha256:" + "e" * 64,
        "calendar_version": "4.13.2",
        "forecast_session_opens_utc": [
            "2026-01-02T14:30:00+00:00",
            "2026-01-05T14:30:00+00:00",
        ][:horizon],
        "input_policy": {
            "schema": "kronos.input-policy.v1",
            "columns": ["open", "high", "low", "close", "volume"],
            "interval": "1d",
            "lookback": 400,
            "history_start_policy": "exact_calendar_session_window",
        },
        "runtime_policy": {
            "implementation_revision": "2",
            "implementation_hash": "a" * 64,
            "device_policy": "cpu_only",
            "deterministic_algorithms": True,
        },
    }
    data = {
        "ok": True,
        "schema": "kronos.forecast.v2",
        "model": "Kronos-test",
        "forecast": forecast,
        "dates": dates,
        "forecast_digest": forecast_digest
        or hashlib.sha256(
            json.dumps(forecast, separators=(",", ":")).encode()
        ).hexdigest(),
        "cache_key": hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "request": request,
        "runtime": request["runtime"],
        "decision_gate": {"status": gate, "reasons": ["test gate"]},
        "generated_at": "2026-01-01T20:00:00+00:00",
    }
    data["artifact_hash"] = "sha256:" + hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return data


def _reseal_payload(data: dict) -> None:
    data["cache_key"] = hashlib.sha256(
        json.dumps(data["request"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    data["artifact_hash"] = "sha256:" + hashlib.sha256(
        json.dumps(
            {key: value for key, value in data.items() if key != "artifact_hash"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _validated_context(data: dict) -> dict:
    request = data["request"]
    bars = []
    for row in request["input_snapshot"]["rows"]:
        values = [float.fromhex(value) for value in row["values_hex"]]
        bars.append(
            dict(
                zip(
                    ["open", "high", "low", "close", "volume"],
                    values,
                ),
                date=row["timestamp"][:10],
            )
        )
    identity = {
        "symbol": request["symbol"],
        "market": "XNAS",
        "interval": "1d",
        "calendar": request["calendar"],
        "currency": "USD",
        "price_basis": "raw",
        "volume_unit": "shares",
    }
    reference = {
        "schema": "stammtisch.validated-bars-reference.v1",
        "bars_sha256": "a" * 64,
        "output_sha256": "b" * 64,
        "identity": identity,
        "calendar": {"name": request["calendar"], "sessions_sha256": "c" * 64},
    }
    return {
        "reference": reference,
        "bars": bars,
        "calendar_sessions": [bar["date"] for bar in bars],
    }


class TimeseriesDriverTest(unittest.TestCase):
    def test_unavailable_without_cmd(self):
        r = TimeseriesDriver("").forecast("AAPL")
        self.assertFalse(r["ok"])
        self.assertIn("no forecast command configured", r["error"])

    def test_legacy_output_is_fail_closed(self):
        cmd = _fake_cmd(json.dumps({"model": "kronos-test", "forecast": [1.5, 2.5, 3.5]}))
        r = TimeseriesDriver(cmd).forecast("AAPL", horizon=3)
        self.assertFalse(r["ok"])
        self.assertTrue(r["execution_ok"])
        self.assertEqual(r["model"], "kronos-test")
        self.assertEqual(r["provenance"]["decision_gate"]["status"], "ABSTAIN")

    def test_bad_json(self):
        r = TimeseriesDriver(_fake_cmd("not json at all")).forecast("AAPL")
        self.assertFalse(r["ok"])
        self.assertIn("not JSON", r["error"])

    def test_missing_forecast_key(self):
        r = TimeseriesDriver(_fake_cmd(json.dumps({"model": "x"}))).forecast("AAPL")
        self.assertFalse(r["ok"])
        self.assertIn("forecast", r["error"])

    def test_non_numeric_forecast_rejected(self):
        r = TimeseriesDriver(_fake_cmd(json.dumps({"forecast": ["a"]}))).forecast("AAPL")
        self.assertFalse(r["ok"])

    def test_dates_length_mismatch(self):
        cmd = _fake_cmd(json.dumps(
            {"forecast": [1.0], "dates": ["2026-01-01", "2026-01-02"]}))
        r = TimeseriesDriver(cmd).forecast("AAPL", horizon=1)
        self.assertFalse(r["ok"])
        self.assertIn("dates", r["error"])

    def test_wrong_horizon_length_rejected(self):
        cmd = _fake_cmd(json.dumps({"forecast": [1.0]}))
        r = TimeseriesDriver(cmd).forecast("AAPL", horizon=2)
        self.assertFalse(r["ok"])
        self.assertIn("horizon", r["error"])

    def test_bool_nan_and_non_positive_rejected(self):
        for value in (True, float("nan"), float("inf"), 0.0, -1.0):
            with self.subTest(value=value):
                cmd = _fake_cmd(json.dumps({"forecast": [value]}))
                self.assertFalse(TimeseriesDriver(cmd).forecast("AAPL", horizon=1)["ok"])

    def test_nonzero_exit_rejected_even_with_forecast(self):
        cmd = _fake_cmd(json.dumps({"forecast": [1.0]}), exit_code=7)
        r = TimeseriesDriver(cmd).forecast("AAPL", horizon=1)
        self.assertFalse(r["ok"])
        self.assertIn("failed", r["error"])

    def test_ok_false_rejected(self):
        cmd = _fake_cmd(json.dumps({"ok": False, "error": "upstream failure", "forecast": [1.0]}))
        r = TimeseriesDriver(cmd).forecast("AAPL", horizon=1)
        self.assertFalse(r["ok"])
        self.assertIn("upstream failure", r["error"])

    def test_dates_must_be_iso_ordered_unique(self):
        for dates in (
            ["not-a-date"],
            ["2026-01-02", "2026-01-01"],
            ["2026-01-01", "2026-01-01"],
        ):
            with self.subTest(dates=dates):
                forecast = [1.0] * len(dates)
                cmd = _fake_cmd(json.dumps({"forecast": forecast, "dates": dates}))
                self.assertFalse(
                    TimeseriesDriver(cmd).forecast("AAPL", horizon=len(dates))["ok"]
                )

    def test_kronos_v2_preserves_abstain_and_provenance(self):
        data = _v2_payload(gate="ABSTAIN")
        r = TimeseriesDriver(_fake_cmd(json.dumps(data))).forecast("AAPL", horizon=2)
        self.assertFalse(r["ok"])
        self.assertTrue(r["execution_ok"])
        # The diagnostic curve rides along, always gated non-PASS.
        self.assertEqual(r["forecast"], data["forecast"])
        self.assertEqual(r["dates"], data["dates"])
        self.assertEqual(r["provenance"]["decision_gate"]["status"], "ABSTAIN")
        self.assertEqual(r["provenance"]["request"], data["request"])
        self.assertEqual(r["provenance"]["forecast_digest"], data["forecast_digest"])

    def test_validated_forecast_snapshot_exact_tail_is_linked(self):
        data = _v2_payload(gate="ABSTAIN")
        context = _validated_context(data)
        r = TimeseriesDriver(_fake_cmd(json.dumps(data))).forecast(
            "AAPL", horizon=2, validated_context=context
        )
        self.assertFalse(r["ok"])
        self.assertTrue(r["execution_ok"])
        self.assertEqual(
            context["reference"],
            r["provenance"]["validated_bars_reference"],
        )

    def test_validated_forecast_rejects_value_date_and_identity_mismatch(self):
        mutations = (
            lambda context: context["bars"][-1].__setitem__("close", 999.0),
            lambda context: context["bars"][-1].__setitem__("date", "2025-12-29"),
            lambda context: context["reference"]["identity"].__setitem__(
                "symbol", "MSFT"
            ),
            lambda context: context["reference"]["identity"].__setitem__(
                "calendar", "XSHG"
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                data = _v2_payload(gate="ABSTAIN")
                context = _validated_context(data)
                mutate(context)
                r = TimeseriesDriver(_fake_cmd(json.dumps(data))).forecast(
                    "AAPL", horizon=2, validated_context=context
                )
                self.assertFalse(r["ok"])
                self.assertNotIn("validated_bars_reference", r.get("provenance", {}))
                self.assertRegex(r["error"], "validated bars|forecast (symbol|calendar|input)")

    def test_kronos_v2_cannot_self_assert_pass(self):
        data = _v2_payload(gate="PASS")
        r = TimeseriesDriver(_fake_cmd(json.dumps(data))).forecast("AAPL", horizon=2)
        self.assertFalse(r["ok"])
        self.assertIn("cannot assert PASS", r["error"])

    def test_kronos_v2_digest_tampering_rejected(self):
        data = _v2_payload(horizon=1, gate="ABSTAIN", forecast_digest="wrong")
        r = TimeseriesDriver(_fake_cmd(json.dumps(data))).forecast("AAPL", horizon=1)
        self.assertFalse(r["ok"])
        self.assertIn("digest", r["error"])

    def test_kronos_v2_artifact_tampering_rejected(self):
        data = _v2_payload(gate="ABSTAIN")
        data["model"] = "tampered"
        r = TimeseriesDriver(_fake_cmd(json.dumps(data))).forecast("AAPL", horizon=2)
        self.assertFalse(r["ok"])
        self.assertIn("artifact hash", r["error"])

    def test_kronos_v2_empty_dates_rejected(self):
        data = _v2_payload(horizon=1, gate="ABSTAIN")
        data["dates"] = []
        data["request"]["forecast_dates"] = []
        data["cache_key"] = hashlib.sha256(
            json.dumps(
                data["request"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        data["artifact_hash"] = "sha256:" + hashlib.sha256(
            json.dumps(
                {key: value for key, value in data.items() if key != "artifact_hash"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        r = TimeseriesDriver(_fake_cmd(json.dumps(data))).forecast("AAPL", horizon=1)
        self.assertFalse(r["ok"])
        self.assertIn("dates", r["error"])

    def test_kronos_v2_incomplete_request_rejected(self):
        data = _v2_payload(gate="ABSTAIN")
        data["request"].pop("input_snapshot")
        data["cache_key"] = hashlib.sha256(
            json.dumps(
                data["request"], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        data["artifact_hash"] = "sha256:" + hashlib.sha256(
            json.dumps(
                {key: value for key, value in data.items() if key != "artifact_hash"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        r = TimeseriesDriver(_fake_cmd(json.dumps(data))).forecast("AAPL", horizon=2)
        self.assertFalse(r["ok"])
        self.assertIn("shape", r["error"])

    def test_kronos_v2_calendar_and_policy_tampering_rejected(self):
        mutations = (
            lambda request: request.__setitem__(
                "calendar_schedule_sha256", "0" * 64
            ),
            lambda request: request.__setitem__("calendar_version", ""),
            lambda request: request.__setitem__(
                "forecast_session_opens_utc", ["2026-01-02T14:30:00+00:00"]
            ),
            lambda request: request.__setitem__(
                "forecast_session_opens_utc", ["not-an-instant"] * 2
            ),
            lambda request: request["input_policy"].__setitem__("lookback", 1),
            lambda request: request["input_policy"].__setitem__(
                "interval", "1h"
            ),
            lambda request: request["runtime_policy"].__setitem__(
                "implementation_hash", "f" * 64
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                data = _v2_payload(gate="ABSTAIN")
                mutate(data["request"])
                _reseal_payload(data)
                r = TimeseriesDriver(_fake_cmd(json.dumps(data))).forecast(
                    "AAPL", horizon=2
                )
                self.assertFalse(r["ok"])
                self.assertRegex(
                    r["error"],
                    "calendar|session opens|input policy|runtime policy",
                )

    def test_kronos_v2_snapshot_semantics_are_replayed(self):
        mutations = (
            lambda request: request.__setitem__("input_hash", "0" * 64),
            lambda request: request.__setitem__("input_rows", 99),
            lambda request: request.__setitem__("input_end", "2025-12-29T00:00:00"),
            lambda request: request["input_snapshot"]["rows"][1][
                "values_hex"
            ].__setitem__(1, float(99.0).hex()),
            lambda request: request["input_snapshot"]["rows"][1].__setitem__(
                "timestamp", "2025-12-30T00:00:00"
            ),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                data = _v2_payload(gate="ABSTAIN")
                mutate(data["request"])
                _reseal_payload(data)
                r = TimeseriesDriver(_fake_cmd(json.dumps(data))).forecast(
                    "AAPL", horizon=2
                )
                self.assertFalse(r["ok"])
                self.assertIn("input", r["error"].lower())

    def test_missing_binary(self):
        r = TimeseriesDriver("/nonexistent/forecast-bin").forecast("AAPL")
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["error"])

    def test_bad_cmd_syntax(self):
        r = TimeseriesDriver('unclosed "quote').forecast("AAPL")
        self.assertFalse(r["ok"])
        self.assertIn("kronos_cmd", r["error"])


if __name__ == "__main__":
    unittest.main()
