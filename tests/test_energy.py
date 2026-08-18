"""ENERGY watchlist — URL building, parsing, formatting, fail-closed egress (offline)."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

from tui import energy as eg


WTI_SPEC = eg.SeriesSpec(
    key="wti_spot",
    group="CRUDE",
    label="WTI Cushing spot",
    route="petroleum/pri/spt",
    facets=(("series", "RWTC"),),
    frequency="daily",
    unit="$/bbl",
)
STOCKS_SPEC = eg.SeriesSpec(
    key="us_crude_stocks",
    group="CRUDE",
    label="US crude stocks ex-SPR",
    route="seriesid/PET.WCESTUS1.W",
    unit="Mbbl",
    decimals=0,
)

V2_PAYLOAD = {
    "response": {
        "total": "2",
        "frequency": "daily",
        "data": [
            {
                "period": "2026-08-13",
                "series": "RWTC",
                "series-description": "Cushing, OK WTI Spot Price FOB",
                "value": "64.40",
                "unit": "$/bbl",
            },
            {
                "period": "2026-08-14",
                "series": "RWTC",
                "series-description": "Cushing, OK WTI Spot Price FOB",
                "value": "63.96",
                "unit": "$/bbl",
            },
        ],
    },
    "apiVersion": "2.1.12",
}


class BuildUrlTest(unittest.TestCase):
    def test_data_route_with_facets_and_sort(self) -> None:
        url = eg.build_url(WTI_SPEC, "KEY123", length=10)
        self.assertTrue(url.startswith(f"{eg.EIA_BASE}/petroleum/pri/spt/data?"))
        self.assertIn("api_key=KEY123", url)
        self.assertIn("frequency=daily", url)
        self.assertIn("data%5B%5D=value", url)
        self.assertIn("facets%5Bseries%5D%5B%5D=RWTC", url)
        self.assertIn("sort%5B0%5D%5Bcolumn%5D=period", url)
        self.assertIn("sort%5B0%5D%5Bdirection%5D=desc", url)
        self.assertIn("length=10", url)

    def test_seriesid_route_has_no_data_suffix(self) -> None:
        url = eg.build_url(STOCKS_SPEC, "KEY123")
        self.assertTrue(url.startswith(f"{eg.EIA_BASE}/seriesid/PET.WCESTUS1.W?"))
        self.assertNotIn("/data?", url)

    def test_blank_frequency_is_omitted(self) -> None:
        url = eg.build_url(STOCKS_SPEC, "KEY123")
        self.assertNotIn("frequency=", url)


class ParseRowsTest(unittest.TestCase):
    def test_parses_latest_previous_and_change(self) -> None:
        row = eg.parse_rows(WTI_SPEC, V2_PAYLOAD)
        self.assertIsNone(row["error"])
        self.assertEqual(row["period"], "2026-08-14")
        self.assertEqual(row["value"], 63.96)
        self.assertEqual(row["prev_period"], "2026-08-13")
        self.assertEqual(row["prev_value"], 64.40)
        self.assertAlmostEqual(row["change"], -0.44, places=6)
        self.assertAlmostEqual(row["change_pct"], -0.44 / 64.40 * 100.0, places=6)
        self.assertEqual(row["description"], "Cushing, OK WTI Spot Price FOB")
        self.assertEqual(
            row["history"], [("2026-08-14", 63.96), ("2026-08-13", 64.40)]
        )

    def test_single_point_has_no_change(self) -> None:
        payload = {"response": {"data": [V2_PAYLOAD["response"]["data"][0]]}}
        row = eg.parse_rows(WTI_SPEC, payload)
        self.assertIsNone(row["error"])
        self.assertIsNone(row["change"])
        self.assertIsNone(row["change_pct"])

    def test_null_and_malformed_points_are_skipped(self) -> None:
        payload = {
            "response": {
                "data": [
                    {"period": "2026-08-14", "value": None},
                    {"period": "2026-08-13", "value": "64.40"},
                    {"period": "", "value": "1.0"},
                    "not-a-dict",
                ]
            }
        }
        row = eg.parse_rows(WTI_SPEC, payload)
        self.assertIsNone(row["error"])
        self.assertEqual(row["period"], "2026-08-13")
        self.assertEqual(len(row["history"]), 1)

    def test_missing_data_block_is_an_error_row(self) -> None:
        row = eg.parse_rows(WTI_SPEC, {"response": {}})
        self.assertIsNotNone(row["error"])
        self.assertEqual(row["key"], "wti_spot")
        self.assertIsNone(row["value"])

    def test_empty_data_is_an_error_row(self) -> None:
        row = eg.parse_rows(WTI_SPEC, {"response": {"data": []}})
        self.assertIn("no usable observations", row["error"])


STEO_SPEC = eg.SeriesSpec(
    key="steo_brent",
    group="OUTLOOK",
    label="Brent · EIA STEO fcst",
    route="steo",
    facets=(("seriesId", "BREPUUS"),),
    frequency="monthly",
    unit="$/bbl",
    forecast=True,
)

STEO_PAYLOAD = {
    "response": {
        "frequency": "monthly",
        "data": [
            {"period": "2026-07", "value": "70.10"},
            {"period": "2026-08", "value": "69.50"},
            {"period": "2026-09", "value": "68.90"},
            {"period": "2026-10", "value": "68.00"},
        ],
    }
}


class ForecastTest(unittest.TestCase):
    def test_headlines_earliest_projection_month(self) -> None:
        row = eg.parse_rows(STEO_SPEC, STEO_PAYLOAD, today=eg.date(2026, 8, 17))
        self.assertIsNone(row["error"])
        self.assertEqual(row["period"], "2026-09")
        self.assertEqual(row["value"], 68.90)
        self.assertEqual(row["prev_period"], "2026-08")
        self.assertEqual(row["prev_value"], 69.50)
        self.assertAlmostEqual(row["change"], -0.60, places=6)

    def test_falls_back_to_latest_when_no_projection(self) -> None:
        row = eg.parse_rows(STEO_SPEC, STEO_PAYLOAD, today=eg.date(2027, 1, 1))
        self.assertEqual(row["period"], "2026-10")
        self.assertEqual(row["prev_period"], "2026-09")

    def test_forecast_url_starts_last_month_and_widens_window(self) -> None:
        url = eg.build_url(STEO_SPEC, "KEY123", length=10)
        self.assertIn("start=", url)
        self.assertIn("length=36", url)


class FormatTest(unittest.TestCase):
    def test_format_value(self) -> None:
        self.assertEqual(eg.format_value(63.956), "63.96")
        self.assertEqual(eg.format_value(423400.0, 0), "423,400")
        self.assertEqual(eg.format_value(None), "—")

    def test_format_change(self) -> None:
        row = eg.parse_rows(WTI_SPEC, V2_PAYLOAD)
        self.assertEqual(eg.format_change(row), "-0.44 (-0.7%)")
        row_up = dict(row, change=0.5, change_pct=0.78)
        self.assertEqual(eg.format_change(row_up), "+0.50 (+0.8%)")
        self.assertEqual(eg.format_change(dict(row, change=None, change_pct=None)), "—")

    def test_format_detail(self) -> None:
        row = eg.parse_rows(WTI_SPEC, V2_PAYLOAD)
        detail = eg.format_detail(row)
        self.assertIn("WTI Cushing spot", detail)
        self.assertIn("2026-08-14 63.96", detail)
        self.assertIn("EIA Open Data API v2", detail)
        err = eg.format_detail(eg._error_row(WTI_SPEC, "HTTP 403"))
        self.assertIn("[ERROR] HTTP 403", err)


class ProxyValidationTest(unittest.TestCase):
    def test_rejects_non_http_and_credentialed_proxies(self) -> None:
        with self.assertRaises(ValueError):
            eg.as_http_proxy("")
        with self.assertRaises(ValueError):
            eg.as_http_proxy("https://proxy.example:8080")
        with self.assertRaises(ValueError):
            eg.as_http_proxy("http://user:pw@proxy.example:8080")
        self.assertEqual(
            eg.as_http_proxy(" http://proxy.example:8080 "),
            "http://proxy.example:8080",
        )

    def test_resolve_egress_prefers_argument_then_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(eg.resolve_egress(None))
        with mock.patch.dict(os.environ, {"STAMMTISCH_ENERGY_PROXY": "http://p1:8080"}):
            self.assertEqual(eg.resolve_egress(None).url, "http://p1:8080")
            self.assertEqual(eg.resolve_egress("http://p2:8080").url, "http://p2:8080")
        with mock.patch.dict(os.environ, {"STAMMTISCH_ENERGY_PROXY": "not-a-url"}):
            self.assertIsNone(eg.resolve_egress(None))


class FetchSeriesTest(unittest.TestCase):
    def test_success_path(self) -> None:
        captured = {}

        def fake_open(url, proxy, timeout=20.0):
            captured["url"] = url
            captured["proxy"] = proxy
            return json.dumps(V2_PAYLOAD).encode("utf-8")

        with mock.patch.object(eg, "_open", fake_open):
            row = eg.fetch_series(WTI_SPEC, "KEY123", "http://proxy.example:8080")
        self.assertIsNone(row["error"])
        self.assertEqual(row["value"], 63.96)
        self.assertEqual(captured["proxy"], "http://proxy.example:8080")
        self.assertIn("api_key=KEY123", captured["url"])

    def test_http_error_degrades_to_error_row(self) -> None:
        def fake_open(url, proxy, timeout=20.0):
            raise HTTPError(url, 403, "Forbidden", {}, None)

        with mock.patch.object(eg, "_open", fake_open):
            row = eg.fetch_series(WTI_SPEC, "KEY123", "http://proxy.example:8080")
        self.assertEqual(row["error"], "HTTP 403")

    def test_network_error_copy_hides_endpoint_details(self) -> None:
        with mock.patch.object(
            eg, "_open", side_effect=URLError(TimeoutError("timed out"))
        ):
            row = eg.fetch_series(WTI_SPEC, "KEY123", "http://proxy.example:8080")
        self.assertEqual(row["error"], "request timed out through the configured proxy")

    def test_invalid_json(self) -> None:
        with mock.patch.object(eg, "_open", return_value=b"not-json"):
            row = eg.fetch_series(WTI_SPEC, "KEY123", "http://proxy.example:8080")
        self.assertEqual(row["error"], "EIA returned invalid JSON")

    def test_eia_error_payload(self) -> None:
        payload = {"error": "Invalid frequency 'millennially' provided.", "code": 400}
        with mock.patch.object(eg, "_open", return_value=json.dumps(payload).encode()):
            row = eg.fetch_series(WTI_SPEC, "KEY123", "http://proxy.example:8080")
        self.assertIn("EIA error", row["error"])
        self.assertIn("Invalid frequency", row["error"])


class FetchWatchlistTest(unittest.TestCase):
    def test_fails_closed_without_api_key(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            result = eg.fetch_watchlist(api_key=None, proxy_url="http://p:8080")
        self.assertFalse(result["ok"])
        self.assertIn("no EIA API key", result["error"])
        self.assertEqual(result["rows"], [])

    def test_fails_closed_without_proxy(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            result = eg.fetch_watchlist(api_key="KEY123", proxy_url=None)
        self.assertFalse(result["ok"])
        self.assertIn("no energy proxy configured", result["error"])

    def test_fails_closed_on_invalid_proxy(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            result = eg.fetch_watchlist(api_key="KEY123", proxy_url="https://p:8080")
        self.assertFalse(result["ok"])
        self.assertIn("invalid energy proxy configuration", result["error"])

    def test_env_key_fallback(self) -> None:
        def fake_open(url, proxy, timeout=20.0):
            return json.dumps(V2_PAYLOAD).encode("utf-8")

        env = {"EIA_API_KEY": "ENVKEY", "STAMMTISCH_ENERGY_PROXY": "http://p:8080"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(eg, "_open", fake_open):
                result = eg.fetch_watchlist(specs=(WTI_SPEC,))
        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"][0]["value"], 63.96)

    def test_partial_failure_keeps_board_ok(self) -> None:
        def fake_open(url, proxy, timeout=20.0):
            if "RWTC" in url:
                return json.dumps(V2_PAYLOAD).encode("utf-8")
            raise URLError("boom")

        specs = (WTI_SPEC, STOCKS_SPEC)
        with mock.patch.object(eg, "_open", fake_open):
            result = eg.fetch_watchlist(
                api_key="KEY123", proxy_url="http://p:8080", specs=specs
            )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["rows"]), 2)
        self.assertIsNone(result["rows"][0]["error"])
        self.assertIsNotNone(result["rows"][1]["error"])

    def test_total_failure_is_not_ok(self) -> None:
        with mock.patch.object(eg, "_open", side_effect=URLError("boom")):
            result = eg.fetch_watchlist(
                api_key="KEY123", proxy_url="http://p:8080", specs=(WTI_SPEC,)
            )
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["rows"]), 1)
        self.assertIsNotNone(result["rows"][0]["error"])

    def test_curated_series_have_unique_keys_and_known_groups(self) -> None:
        keys = [spec.key for spec in eg.SERIES]
        self.assertEqual(len(keys), len(set(keys)))
        for spec in eg.SERIES:
            self.assertIn(spec.group, {"CRUDE", "GAS", "COAL", "WORLD", "OUTLOOK"})
            self.assertTrue(spec.route)
            self.assertTrue(spec.label)


if __name__ == "__main__":
    unittest.main()
