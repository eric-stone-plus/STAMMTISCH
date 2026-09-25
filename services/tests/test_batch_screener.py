"""Batch screener degradation — geo-block and transport failures (offline).

The crypto universe lives on one keyless endpoint; when that endpoint
refuses the configured egress the screener must degrade with an
actionable, structured error instead of a bare transport traceback.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root

from services import batch_screener

TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"


def _config() -> SimpleNamespace:
    return SimpleNamespace(data_proxy_url="", egress_proxy_url="")


def _http_error(code: int) -> HTTPError:
    return HTTPError(TICKER_URL, code, f"status {code}", None, None)


class CryptoScreenDegradationTest(unittest.TestCase):
    def test_geo_block_451_is_actionable(self):
        with mock.patch("services.datafeeds.http.get_json",
                        side_effect=_http_error(451)):
            result = batch_screener.crypto_screen(_config())
        self.assertFalse(result["ok"])
        self.assertIn("451", result["error"])
        self.assertIn("geo-block", result["error"])
        # The remedy names the two operator levers, no host details.
        self.assertIn("egress", result["error"])
        self.assertIn("data_proxy_url", result["error"])

    def test_other_http_error_names_the_code(self):
        with mock.patch("services.datafeeds.http.get_json",
                        side_effect=_http_error(503)):
            result = batch_screener.crypto_screen(_config())
        self.assertFalse(result["ok"])
        self.assertIn("503", result["error"])

    def test_unreachable_transport_degrades(self):
        with mock.patch("services.datafeeds.http.get_json",
                        side_effect=URLError(ConnectionRefusedError())):
            result = batch_screener.crypto_screen(_config())
        self.assertFalse(result["ok"])
        self.assertIn("unreachable", result["error"])

    def test_timeout_degrades(self):
        with mock.patch("services.datafeeds.http.get_json",
                        side_effect=TimeoutError("timed out")):
            result = batch_screener.crypto_screen(_config())
        self.assertFalse(result["ok"])
        self.assertIn("unreachable", result["error"])


def _kline_closes() -> list:
    """Synthetic 1h zigzag closes that produce >=5 RSI(2) round trips."""
    import numpy as np

    closes = np.tile([100.0, 99.0, 88.0, 90.0, 95.0, 99.0], 25)
    return [[0, 0, 0, 0, float(c), 0] for c in closes]


def _tickers(count: int) -> list:
    return [{"symbol": f"C{i:02d}USDT", "quoteVolume": str(100 - i)}
            for i in range(count)]


class KlinesDegradationTest(unittest.TestCase):
    """Per-symbol transport failures must not pass as universe truth."""

    def test_klines_geo_block_fails_closed(self):
        def fake(url, **kwargs):
            if "ticker" in url:
                return _tickers(3)
            raise HTTPError(url, 451, "Unavailable For Legal Reasons",
                            None, None)

        with mock.patch("services.datafeeds.http.get_json",
                        side_effect=fake):
            result = batch_screener.crypto_screen(_config(), min_volume=2)
        self.assertFalse(result["ok"])
        self.assertIn("451", result["error"])
        self.assertIn("klines", result["error"])
        self.assertEqual(result["evaluated"], 0)
        self.assertNotIn("fee_tiers", result)  # NaN medians never computed

    def test_near_universal_failure_floor_refuses_survivor_stats(self):
        def fake(url, **kwargs):
            if "ticker" in url:
                return _tickers(10)
            if "C00USDT" in url:
                return _kline_closes()
            raise HTTPError(url, 451, "blocked", None, None)

        with mock.patch("services.datafeeds.http.get_json",
                        side_effect=fake):
            result = batch_screener.crypto_screen(_config(), min_volume=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["evaluated"], 1)  # the survivor is reported…
        self.assertIn("451", result["error"])     # …but never as a statistic

    def test_healthy_sweep_still_returns_tiers(self):
        def fake(url, **kwargs):
            if "ticker" in url:
                return _tickers(2)
            return _kline_closes()

        with mock.patch("services.datafeeds.http.get_json",
                        side_effect=fake):
            result = batch_screener.crypto_screen(_config(), min_volume=2)
        self.assertNotIn("ok", result)
        self.assertEqual(result["universe"], 2)
        self.assertEqual(result["evaluated"], 2)
        self.assertIn("0.05%", result["fee_tiers"])


class PersistStrictJsonTest(unittest.TestCase):
    def test_nan_payload_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, \
             self.assertRaises(ValueError):
            batch_screener.persist(
                tmp, "crypto",
                {"evaluated": 0, "tiers": {"median_net": float("nan")}})


class StockScreenGuardTest(unittest.TestCase):
    def test_empty_sample_fails_closed(self):
        with mock.patch("services.datafeeds.http.get_json",
                        return_value=[]), \
             mock.patch.object(batch_screener, "AlpacaBroker"):
            result = batch_screener.stock_screen(_config())
        self.assertFalse(result["ok"])
        self.assertEqual(result["evaluated"], 0)


if __name__ == "__main__":
    unittest.main()
