"""Chart server tests — payload shape and a live-server round trip
with a mocked data source (no network)."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
from pathlib import Path
import shlex
import socket
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import tui.chart_server as chart_server

from tui.chart_server import (
    ChartHandler,
    ThreadingHTTPServer,
    candles_payload,
    find_available_port,
    is_running,
    parse_validated_reference,
    validated_candles_payload,
)


def _sample_df(n: int = 3) -> pd.DataFrame:
    idx = pd.date_range("2026-08-10", periods=n, freq="D")
    return pd.DataFrame({
        "open": [10.0, 10.5, 11.0],
        "high": [10.8, 11.2, 11.5],
        "low": [9.8, 10.3, 10.9],
        "close": [10.5, 11.0, 11.2],
        "volume": [1000.0, 2000.0, 1500.0],
    }, index=idx)


def _v2_abstain_payload(horizon: int) -> dict:
    """A sealed forecast receipt suitable for the HTTP integration test."""
    forecast = [101.0 + position for position in range(horizon)]
    dates = [
        value.date().isoformat()
        for value in pd.bdate_range("2026-01-02", periods=horizon)
    ]
    columns = ["open", "high", "low", "close", "volume"]
    snapshot_rows = []
    input_digest = hashlib.sha256(b"kronos-ohlcv-v1\0")
    for day, values in (
        ("2025-12-30T00:00:00", [100.0, 102.0, 99.0, 101.0, 1000.0]),
        ("2025-12-31T00:00:00", [101.0, 103.0, 100.0, 102.0, 1100.0]),
    ):
        encoded = [float(value).hex() for value in values]
        snapshot_rows.append({"timestamp": day, "values_hex": encoded})
        input_digest.update("\x1f".join([day, *encoded]).encode())
        input_digest.update(b"\n")
    runtime = {"device": "cpu", "model_snapshot": {"snapshot_sha256": "a" * 64}}
    request = {
        "schema": "kronos.forecast.v2",
        "implementation_revision": "2",
        "implementation_hash": "b" * 64,
        "symbol": "AAPL",
        "requested_as_of": "2026-01-01T20:00:00Z",
        "decision_cutoff_utc": "2026-01-01T20:00:00+00:00",
        "input_end": "2025-12-31T00:00:00",
        "calendar": "XNYS",
        "last_completed_session": "2025-12-31",
        "forecast_dates": dates,
        "input_hash": input_digest.hexdigest(),
        "input_rows": len(snapshot_rows),
        "input_start": "2025-12-30T00:00:00",
        "input_snapshot": {
            "encoding": "float.hex.v1",
            "columns": columns,
            "rows": snapshot_rows,
        },
        "data": {"provider": "test", "provider_point_in_time": False},
        "runtime": runtime,
        "horizon": horizon,
        "model_id": "test/model",
        "model_revision": "c" * 40,
        "tokenizer_id": "test/tokenizer",
        "tokenizer_revision": "d" * 40,
        "inference": {"seed": 1729},
        "calendar_schedule_sha256": "sha256:" + "e" * 64,
        "calendar_version": "4.13.2",
        "forecast_session_opens_utc": [
            f"{day}T14:30:00+00:00" for day in dates
        ],
        "input_policy": {
            "schema": "kronos.input-policy.v1",
            "columns": columns,
            "interval": "1d",
            "lookback": 400,
            "history_start_policy": "exact_calendar_session_window",
        },
        "runtime_policy": {
            "implementation_revision": "2",
            "implementation_hash": "b" * 64,
            "device_policy": "cpu_only",
            "deterministic_algorithms": True,
        },
    }
    payload = {
        "ok": True,
        "schema": "kronos.forecast.v2",
        "model": "Kronos-test",
        "forecast": forecast,
        "dates": dates,
        "forecast_digest": hashlib.sha256(
            json.dumps(forecast, separators=(",", ":")).encode()
        ).hexdigest(),
        "cache_key": hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "request": request,
        "runtime": runtime,
        "decision_gate": {"status": "ABSTAIN", "reasons": ["test gate"]},
        "generated_at": "2026-01-01T20:00:00+00:00",
    }
    payload["artifact_hash"] = "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def _forecast_command(root: Path, payload: dict) -> str:
    script = root / "fake_forecast.py"
    script.write_text(f"print({json.dumps(payload)!r})\n", encoding="utf-8")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


class CandlesPayloadTest(unittest.TestCase):
    def test_shape(self):
        payload = candles_payload(_sample_df(), "AAPL")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(len(payload["candles"]), 3)
        first = payload["candles"][0]
        self.assertEqual(first["time"], "2026-08-10")
        self.assertEqual(first["open"], 10.0)
        self.assertEqual(first["volume"], 1000.0)

    def test_dirty_frame_is_sanitized_not_raised(self):
        # Unsorted dates, same calendar day twice, NaN/inf OHLC, then a
        # clean bar — the function /api/candles actually calls.
        idx = pd.to_datetime([
            "2026-01-03",
            "2026-01-01 09:30",
            "2026-01-01 15:00",
            "2026-01-02",
            "2026-01-04",
        ], format="mixed")
        df = pd.DataFrame({
            "open":   [30.0, 10.0, 11.0, float("nan"), 40.0],
            "high":   [31.0, 12.0, 13.0, 20.0, 41.0],
            "low":    [29.0, 9.0, 8.5, 19.0, 39.0],
            "close":  [30.5, 11.0, 12.0, 20.0, 40.5],
            "volume": [100.0, 5.0, 7.0, 1.0, 50.0],
        }, index=idx)
        payload = candles_payload(df, "DIRT")
        self.assertTrue(payload["ok"])
        times = [c["time"] for c in payload["candles"]]
        self.assertEqual(times, sorted(set(times)))
        self.assertEqual(times, ["2026-01-01", "2026-01-03", "2026-01-04"])
        for i in range(1, len(times)):
            self.assertLess(times[i - 1], times[i])
        for bar in payload["candles"]:
            for key in ("open", "high", "low", "close", "volume"):
                self.assertTrue(math.isfinite(bar[key]))
        first = payload["candles"][0]
        self.assertEqual(first["time"], "2026-01-01")
        self.assertEqual(first["open"], 10.0)
        self.assertEqual(first["high"], 13.0)
        self.assertEqual(first["low"], 8.5)
        self.assertEqual(first["close"], 12.0)
        self.assertEqual(first["volume"], 12.0)

    def test_non_timestamp_index_does_not_raise(self):
        ranged = pd.DataFrame({
            "open": [1.0, 2.0],
            "high": [1.5, 2.5],
            "low": [0.5, 1.5],
            "close": [1.2, 2.2],
            "volume": [10.0, 20.0],
        })
        ranged_payload = candles_payload(ranged, "RANGE")
        self.assertFalse(ranged_payload["ok"])
        self.assertEqual(ranged_payload["candles"], [])
        self.assertIn("error", ranged_payload)

        strings = pd.DataFrame({
            "open": [1.0, float("inf"), 3.0],
            "high": [1.2, 9.0, 3.2],
            "low": [0.8, 1.0, 2.8],
            "close": [1.1, 2.0, 3.1],
            "volume": [4.0, 5.0, 6.0],
        }, index=["2026-02-02", "2026-02-01", "2026-02-03"])
        str_payload = candles_payload(strings, "STR")
        self.assertTrue(str_payload["ok"])
        times = [c["time"] for c in str_payload["candles"]]
        self.assertEqual(times, ["2026-02-02", "2026-02-03"])
        for bar in str_payload["candles"]:
            for key in ("open", "high", "low", "close", "volume"):
                self.assertTrue(math.isfinite(bar[key]))

    def test_validated_payload_preserves_exact_records_and_provenance(self):
        identity = {
            "symbol": "600584.SS",
            "market": "XSHG",
            "interval": "1d",
            "calendar": "XSHG",
            "currency": "CNY",
            "price_basis": "raw",
            "volume_unit": "shares",
        }
        result = SimpleNamespace(
            records=(
                {
                    "date": "2026-08-14",
                    "open": 51.8,
                    "high": 53.1,
                    "low": 51.5,
                    "close": 52.9,
                    "volume": 1_500_000,
                },
            ),
            manifest={
                "identity": identity,
                "bars_sha256": "a" * 64,
                "output_sha256": "b" * 64,
                "calendar": {
                    "name": "XSHG",
                    "sessions": ["2026-08-14"],
                    "sessions_sha256": "c" * 64,
                },
            },
        )
        with mock.patch("tui.validated_bars.ValidatedBarsStore") as store_cls:
            store_cls.return_value.lookup.return_value = result
            payload = validated_candles_payload(
                "600584.SS", "XSHG", "/evidence/consensus"
            )

        store_cls.assert_called_once_with("/evidence/consensus")
        store_cls.return_value.lookup.assert_called_once_with(
            "600584.SS", market="XSHG", interval="1d"
        )
        self.assertEqual("2026-08-14", payload["candles"][0]["time"])
        self.assertNotIn("date", payload["candles"][0])
        self.assertEqual(52.9, payload["candles"][0]["close"])
        self.assertEqual(
            {
                "data_mode": "validated",
                "bars_sha256": "a" * 64,
                "output_sha256": "b" * 64,
                "identity": identity,
                "reference": {
                    "schema": "stammtisch.validated-bars-reference.v1",
                    "bars_sha256": "a" * 64,
                    "output_sha256": "b" * 64,
                    "identity": identity,
                    "calendar": {
                        "name": "XSHG",
                        "sessions_sha256": "c" * 64,
                    },
                },
            },
            payload["provenance"],
        )

    def test_validated_market_query_accepts_exact_mic_identity(self):
        result = SimpleNamespace(
            records=(
                {
                    "date": "2026-08-14",
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 0,
                },
            ),
            manifest={
                "identity": {
                    "symbol": "H30184",
                    "market": "XSHG",
                    "interval": "1d",
                    "calendar": "XSHG",
                    "currency": "CNY",
                    "price_basis": "raw",
                    "volume_unit": "shares",
                },
                "bars_sha256": "a" * 64,
                "output_sha256": "b" * 64,
                "calendar": {
                    "name": "XSHG",
                    "sessions": ["2026-08-14"],
                    "sessions_sha256": "c" * 64,
                },
            },
        )
        with mock.patch("tui.validated_bars.ValidatedBarsStore") as store_cls:
            store_cls.return_value.lookup.return_value = result
            payload = validated_candles_payload("H30184", "XSHG", "/evidence")

        store_cls.return_value.lookup.assert_called_once_with(
            "H30184", market="XSHG", interval="1d"
        )
        self.assertEqual("H30184", payload["symbol"])
        self.assertEqual("XSHG", payload["mic"])

        for invalid in ("xshg", "cn", "auto", ""):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validated_candles_payload("H30184", invalid, "/evidence")

    def test_validated_reference_strict_shape_rejects_tampering(self):
        identity = {
            "symbol": "H30184",
            "market": "XSHG",
            "interval": "1d",
            "calendar": "XSHG",
            "currency": "CNY",
            "price_basis": "raw",
            "volume_unit": "shares",
        }
        reference = {
            "schema": "stammtisch.validated-bars-reference.v1",
            "bars_sha256": "a" * 64,
            "output_sha256": "b" * 64,
            "identity": identity,
            "calendar": {"name": "XSHG", "sessions_sha256": "c" * 64},
        }
        self.assertEqual(reference, parse_validated_reference(json.dumps(reference)))
        reference["bars_sha256"] = "tampered"
        with self.assertRaisesRegex(ValueError, "bars_sha256"):
            parse_validated_reference(json.dumps(reference))


class LiveServerTest(unittest.TestCase):
    def test_round_trip(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ChartHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

            # Index page serves HTML.
            conn.request("GET", "/")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.getheader("Content-Type"))
            html = resp.read().decode()
            self.assertIn("lightweight-charts.standalone.production.js", html)
            # Math is inlined so a stale server that cannot serve a new
            # /static/*.js route still defines ChartMath.
            self.assertIn("function pathSymbol", html)
            self.assertIn("function legendSnapshot", html)
            self.assertIn('<input id="symbol"', html)
            self.assertNotIn("placeholder=", html)
            self.assertNotIn("barsBeforeCurrent", html)
            self.assertNotIn('x="7.5"', html)
            self.assertNotIn('<rect x="2"', html)
            self.assertNotIn("brand-mark", html)
            brand = html[html.find('id="brand"'):html.find('id="search-box"')]
            self.assertIn("STAMMTISCH", brand)
            self.assertNotIn("<svg", brand)
            self.assertIn("goHome", html)
            self.assertIn("launch-grid", html)
            self.assertIn('class="launch"', html)
            desk = html[html.find('id="desk"'):html.find('id="chart-wrap"')]
            for fixture in (
                "600098.SS", "600519.SS", "300750.SZ", "9992.HK", "0700.HK",
                "AAPL", "NVDA", "7203.T", "005930.KS",
            ):
                self.assertNotIn('data-sym="%s"' % fixture, desk)
            self.assertIn("HISTORY", html)
            self.assertIn("function historyRecord", html)
            self.assertNotIn("horizon=20", html)
            self.assertIn('id="provenance"', html)
            self.assertIn("function setDataProvenance", html)
            self.assertIn('prefix + "bars_sha256="', html)
            self.assertIn('prefix + "output_sha256="', html)
            self.assertIn("validatedProvenanceLeg", html)
            self.assertIn("validated_ref=${encodeURIComponent", html)

            # /chart/<symbol> also serves the app shell — input stays empty.
            conn.request("GET", "/chart/600098")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            chart_html = resp.read().decode()
            self.assertIn('<input id="symbol"', chart_html)
            self.assertNotIn("placeholder=", chart_html)
            self.assertNotRegex(chart_html, r'<input[^>]*id="symbol"[^>]*value=')

            conn.request("GET", "/static/chart_math.js")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            math_js = resp.read().decode()
            self.assertIn("legendSnapshot", math_js)
            self.assertIn("forecastPoints", math_js)

            conn.request("GET", "/api/search?q=tencent")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            search = json.loads(resp.read())
            self.assertTrue(search["ok"])
            self.assertTrue(any(h["symbol"] == "0700.HK" for h in search["hits"]))
            self.assertTrue(any(h["market"] == "HK" for h in search["hits"]))

            # Candles endpoint with a mocked data source: bare 6-digit
            # codes get their exchange suffix, and the payload matches.
            with mock.patch("quantkit.data.fetch_ohlcv",
                            return_value=_sample_df()) as fetch:
                conn.request("GET", "/api/candles?symbol=600098&start=2026-08-01")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read())
                self.assertTrue(data["ok"])
                self.assertEqual(data["symbol"], "600098.SS")
                self.assertEqual(len(data["candles"]), 3)
                fetch.assert_called_once()
                self.assertIn("600098.SS", str(fetch.call_args))

            # fetch_ohlcv returning None is a structured error, not a 500.
            with mock.patch("quantkit.data.fetch_ohlcv", return_value=None):
                conn.request("GET", "/api/candles?symbol=600098")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                none_data = json.loads(resp.read())
                self.assertFalse(none_data["ok"])
                self.assertIn("error", none_data)

            empty = pd.DataFrame(
                {"open": [], "high": [], "low": [], "close": [], "volume": []})
            with mock.patch("quantkit.data.fetch_ohlcv", return_value=empty):
                conn.request("GET", "/api/candles?symbol=600098")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                empty_data = json.loads(resp.read())
                self.assertFalse(empty_data["ok"])

            # Unknown route is a 404.
            conn.request("GET", "/nope")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 404)
            resp.read()
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_is_running_probe(self):
        # A freshly freed ephemeral port with nothing listening on it.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        self.assertFalse(is_running(free_port))

        server = ThreadingHTTPServer(("127.0.0.1", 0), ChartHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(is_running(port))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_validated_mode_failure_never_calls_live_provider(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ChartHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = mock.Mock()
            config.ohlcv_mode = "validated"
            config.validated_bars_root = "/evidence/consensus"
            with (
                mock.patch("tui.chart_server.Config", return_value=config),
                mock.patch("tui.validated_bars.ValidatedBarsStore") as store_cls,
                mock.patch("quantkit.data.fetch_ohlcv") as live_fetch,
            ):
                store_cls.return_value.lookup.side_effect = ValueError("digest drift")
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/api/candles?symbol=600584&mic=XSHG")
                response = conn.getresponse()
                self.assertEqual(200, response.status)
                payload = json.loads(response.read())
                conn.close()

            self.assertFalse(payload["ok"])
            self.assertEqual([], payload["candles"])
            self.assertEqual("validated", payload["provenance"]["data_mode"])
            self.assertIn("digest drift", payload["error"])
            live_fetch.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_real_validated_store_accepts_only_sealed_accepted_manifest(self):
        # Exercise the HTTP boundary against the real verifier. Missing,
        # non-accepted, and digest-drifted manifests must all stop here rather
        # than reaching quantkit's network-capable live fetcher.
        from tests.test_validated_bars import _manifest

        server = ThreadingHTTPServer(("127.0.0.1", 0), ChartHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="stammtisch-chart-bars-") as tmp:
                root = Path(tmp)
                config = mock.Mock()
                config.ohlcv_mode = "validated"
                config.validated_bars_root = str(root)

                def request() -> dict:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", "/api/candles?symbol=600584.SS&mic=XSHG")
                    response = conn.getresponse()
                    self.assertEqual(200, response.status)
                    payload = json.loads(response.read())
                    conn.close()
                    return payload

                with (
                    mock.patch("tui.chart_server.Config", return_value=config),
                    mock.patch("quantkit.data.fetch_ohlcv") as live_fetch,
                ):
                    missing = request()
                    self.assertFalse(missing["ok"])
                    self.assertEqual([], missing["candles"])

                    accepted_manifest = _manifest(symbol="600584.SS", market="ashare")
                    accepted_manifest["identity"]["market"] = "XSHG"
                    for source in accepted_manifest["sources"]:
                        source["identity"]["market"] = "XSHG"
                    from tests.test_validated_bars import _reseal
                    _reseal(accepted_manifest)
                    manifest_path = root / "600584.json"
                    manifest_path.write_text(
                        json.dumps(accepted_manifest, separators=(",", ":")),
                        encoding="utf-8",
                    )
                    accepted = request()
                    self.assertTrue(accepted["ok"])
                    self.assertEqual(
                        [
                            {
                                "time": record["date"],
                                "open": record["open"],
                                "high": record["high"],
                                "low": record["low"],
                                "close": record["close"],
                                "volume": record["volume"],
                            }
                            for record in accepted_manifest["bars"]
                        ],
                        accepted["candles"],
                    )
                    self.assertEqual(
                        accepted_manifest["bars_sha256"],
                        accepted["provenance"]["bars_sha256"],
                    )
                    self.assertEqual(
                        accepted_manifest["output_sha256"],
                        accepted["provenance"]["output_sha256"],
                    )
                    self.assertEqual(
                        accepted_manifest["identity"],
                        accepted["provenance"]["identity"],
                    )

                    for status in ("partial", "quarantined"):
                        with self.subTest(status=status):
                            manifest_path.write_text(
                                json.dumps(
                                    _manifest(
                                        symbol="600584.SS",
                                        market="ashare",
                                        status=status,
                                    ),
                                    separators=(",", ":"),
                                ),
                                encoding="utf-8",
                            )
                            rejected = request()
                            self.assertFalse(rejected["ok"])
                            self.assertEqual([], rejected["candles"])

                    tampered = _manifest(symbol="600584.SS", market="ashare")
                    tampered["identity"]["market"] = "XSHG"
                    for source in tampered["sources"]:
                        source["identity"]["market"] = "XSHG"
                    _reseal(tampered)
                    tampered["bars"][0]["close"] += 1
                    manifest_path.write_text(
                        json.dumps(tampered, separators=(",", ":")),
                        encoding="utf-8",
                    )
                    drifted = request()
                    self.assertFalse(drifted["ok"])
                    self.assertEqual([], drifted["candles"])
                    live_fetch.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_forecast_uses_configured_horizon_and_marks_abstain_diagnostic(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ChartHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="stammtisch-forecast-http-") as tmp:
                command = _forecast_command(Path(tmp), _v2_abstain_payload(3))
                config = mock.Mock()
                config.ohlcv_mode = "live"
                config.get.side_effect = lambda key, default=None: {
                    "kronos_cmd": command,
                    "kronos_horizon": 3,
                }.get(key, default)
                with mock.patch("tui.chart_server.Config", return_value=config):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", "/api/forecast?symbol=AAPL")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    payload = json.loads(response.read())
                    conn.close()

            self.assertFalse(payload["ok"])
            self.assertTrue(payload["execution_ok"])
            # ABSTAIN curves ship as labeled diagnostics, never as evidence.
            self.assertEqual(payload["forecast"], [101.0, 102.0, 103.0])
            self.assertEqual(len(payload["dates"]), 3)
            provenance = payload["provenance"]
            self.assertEqual(provenance["decision_gate"]["status"], "ABSTAIN")
            self.assertEqual(provenance["request"]["horizon"], 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_available_port_is_os_assigned(self):
        port = find_available_port()
        self.assertGreater(port, 0)
        self.assertFalse(is_running(port))


class OwnedServerLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        # Each application owns one irreversible startup/shutdown lifecycle.
        # Reset the module singleton to model a newly constructed application.
        chart_server._owned_process = None
        chart_server._owned_port = None
        chart_server._owned_shutdown = False

    def tearDown(self) -> None:
        chart_server.stop_owned_server()

    def test_authenticated_child_readiness_and_shutdown(self) -> None:
        port = chart_server.ensure_running(0)
        self.assertIsInstance(port, int)
        self.assertGreater(port, 0)
        self.assertTrue(is_running(port))
        process = chart_server._owned_process
        self.assertIsNotNone(process)

        chart_server.stop_owned_server()

        self.assertIsNone(chart_server._owned_process)
        self.assertIsNone(chart_server._owned_port)
        self.assertIsNotNone(process.poll())
        self.assertFalse(is_running(port))

    def test_concurrent_ephemeral_requests_spawn_exactly_one_child(self) -> None:
        original_popen = chart_server.subprocess.Popen
        spawned = []

        def recording_popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            spawned.append(process)
            return process

        with (
            mock.patch.object(
                chart_server.subprocess, "Popen", side_effect=recording_popen
            ),
            ThreadPoolExecutor(max_workers=4) as pool,
        ):
            futures = [pool.submit(chart_server.ensure_running, 0) for _ in range(4)]
            ports = [future.result(timeout=10) for future in futures]

        self.assertEqual(len(spawned), 1)
        self.assertIsInstance(ports[0], int)
        self.assertEqual(ports, [ports[0]] * 4)

    def test_existing_fixed_port_is_never_adopted_or_stopped(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), ChartHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(chart_server.subprocess, "Popen") as spawn:
                self.assertIsNone(chart_server.ensure_running(port))
            spawn.assert_not_called()
            chart_server.stop_owned_server()
            self.assertTrue(is_running(port))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_second_fixed_port_cannot_overwrite_owned_child(self) -> None:
        first_port = chart_server.ensure_running(0)
        first_process = chart_server._owned_process
        second_port = find_available_port()

        with mock.patch.object(chart_server.subprocess, "Popen") as spawn:
            self.assertIsNone(chart_server.ensure_running(second_port))

        spawn.assert_not_called()
        self.assertIs(chart_server._owned_process, first_process)
        self.assertEqual(chart_server._owned_port, first_port)
        self.assertIsNone(first_process.poll())

    def test_ensure_queued_after_shutdown_never_spawns(self) -> None:
        chart_server.stop_owned_server()
        with mock.patch.object(chart_server.subprocess, "Popen") as spawn:
            self.assertIsNone(chart_server.ensure_running(0))
        spawn.assert_not_called()

    def test_listener_appearing_after_preflight_does_not_fake_readiness(self) -> None:
        class FakeProcess:
            pid = 12345
            stdout = StringIO("")

            def __init__(self):
                self.returncode = None
                self.stopped = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.stopped = True
                self.returncode = -15

            def kill(self):
                self.stopped = True
                self.returncode = -9

            def wait(self, timeout=None):
                if self.returncode is None:
                    raise chart_server.subprocess.TimeoutExpired("chart", timeout)
                return self.returncode

        process = FakeProcess()
        with (
            mock.patch.object(chart_server, "is_running", side_effect=[False, True]),
            mock.patch.object(chart_server.subprocess, "Popen", return_value=process),
            mock.patch.object(chart_server, "_stop_process") as stop,
        ):
            self.assertIsNone(chart_server.ensure_running(41234))

        stop.assert_called_once_with(process)
        self.assertIsNone(chart_server._owned_process)
        self.assertIsNone(chart_server._owned_port)

    def test_forged_child_readiness_token_is_rejected(self) -> None:
        class FakeProcess:
            pid = 54321
            stdout = StringIO(json.dumps({
                "schema": chart_server._READY_SCHEMA,
                "token": "wrong-token",
                "port": 41235,
                "pid": 54321,
            }) + "\n")

            def poll(self):
                return None

        process = FakeProcess()
        with (
            mock.patch.object(chart_server, "is_running", return_value=False),
            mock.patch.object(chart_server.subprocess, "Popen", return_value=process),
            mock.patch.object(chart_server, "_stop_process") as stop,
        ):
            self.assertIsNone(chart_server.ensure_running(41235))

        stop.assert_called_once_with(process)
        self.assertIsNone(chart_server._owned_process)


class ExternalBarsTest(unittest.TestCase):
    """SGX:<CODE> external series — fail closed at every step."""

    def _write_bars(self, root: Path, code: str = "MF5F", **overrides) -> Path:
        document = {
            "schema": "mktdaily.bars.v1",
            "code": code,
            "name": "Marine Fuel 0.5% FOB Singapore (VLSFO)",
            "unit": "USD/mt",
            "source": "SGX daily settlement file",
            "bars": [
                {"date": "2026-08-13", "open": 727.73, "high": 727.73,
                 "low": 727.73, "close": 727.73, "volume": 5.0},
                {"date": "2026-08-14", "open": 734.81, "high": 734.81,
                 "low": 734.81, "close": 734.81, "volume": 10.0},
            ],
        }
        document.update(overrides)
        path = root / f"{code}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_unconfigured_root_fails_closed(self):
        payload = chart_server.external_candles_payload("", "SGX:MF5F")
        self.assertFalse(payload["ok"])
        self.assertIn("external_bars_root", payload["error"])

    def test_symbol_shape_is_enforced(self):
        for bad in ("SGX:../../etc/passwd", "SGX:mf5f", "SGX:", "SGX:TOOLONGCODE123"):
            payload = chart_server.external_candles_payload("/tmp", bad)
            self.assertFalse(payload["ok"], bad)
            self.assertIn("invalid external symbol", payload["error"])

    def test_missing_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = chart_server.external_candles_payload(tmp, "SGX:NOPE")
        self.assertFalse(payload["ok"])
        self.assertIn("unavailable", payload["error"])

    def test_wrong_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_bars(Path(tmp), schema="other.v1")
            payload = chart_server.external_candles_payload(tmp, "SGX:MF5F")
        self.assertFalse(payload["ok"])
        self.assertIn("schema", payload["error"])

    def test_valid_file_maps_to_candles(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_bars(Path(tmp))
            payload = chart_server.external_candles_payload(tmp, "SGX:MF5F")
        self.assertTrue(payload["ok"], payload.get("error"))
        self.assertEqual(len(payload["candles"]), 2)
        first = payload["candles"][0]
        self.assertEqual(first["time"], "2026-08-13")
        self.assertEqual(first["close"], 727.73)
        self.assertEqual(payload["provenance"]["data_mode"], "external")
        self.assertEqual(payload["provenance"]["unit"], "USD/mt")


if __name__ == "__main__":
    unittest.main()
