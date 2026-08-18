"""QuantEngine symbol normalization — bare A-share codes get suffixes."""

from __future__ import annotations

import unittest

from unittest import mock

import pandas as pd

from tui.engine import QuantEngine, _missing_ohlcv, _normalize_symbol


class SymbolNormalizeTest(unittest.TestCase):
    def test_bare_shanghai(self):
        self.assertEqual(_normalize_symbol("600098"), "600098.SS")
        self.assertEqual(_normalize_symbol("601318"), "601318.SS")

    def test_bare_shenzhen(self):
        self.assertEqual(_normalize_symbol("000001"), "000001.SZ")
        self.assertEqual(_normalize_symbol("300750"), "300750.SZ")
        self.assertEqual(_normalize_symbol("002594"), "002594.SZ")

    def test_bare_beijing(self):
        self.assertEqual(_normalize_symbol("830799"), "830799.BJ")

    def test_passthrough(self):
        self.assertEqual(_normalize_symbol("AAPL"), "AAPL")
        self.assertEqual(_normalize_symbol("600519.SS"), "600519.SS")
        self.assertEqual(_normalize_symbol("BTC-USD"), "BTC-USD")
        self.assertEqual(_normalize_symbol("0700"), "0700.HK")
        self.assertEqual(_normalize_symbol("7203"), "7203.T")


class MissingOhlcvTest(unittest.TestCase):
    def test_helper_treats_none_and_empty_as_missing(self):
        self.assertTrue(_missing_ohlcv(None))
        self.assertTrue(_missing_ohlcv(pd.DataFrame()))
        self.assertFalse(_missing_ohlcv(pd.DataFrame({"close": [1.0]})))

    def test_fetch_data_none_is_structured_error(self):
        eng = QuantEngine()
        with mock.patch("quantkit.data.fetch_ohlcv", return_value=None):
            r = eng.fetch_data("600098")
        self.assertFalse(r["ok"])
        self.assertIn("No data", r["error"])
        self.assertNotIn("empty", r["error"])

    def test_fetch_data_empty_frame_is_structured_error(self):
        eng = QuantEngine()
        with mock.patch("quantkit.data.fetch_ohlcv", return_value=pd.DataFrame()):
            r = eng.fetch_data("AAPL")
        self.assertFalse(r["ok"])
        self.assertIn("No data", r["error"])

    def test_backtest_indicators_factors_none(self):
        eng = QuantEngine()
        with mock.patch("quantkit.data.fetch_ohlcv", return_value=None):
            bt = eng.run_backtest("600098")
            ind = eng.compute_indicators("600098")
            fac = eng.build_factors("600098")
        self.assertFalse(bt["ok"])
        self.assertIn("No data", bt["error"])
        self.assertFalse(ind["ok"])
        self.assertIn("No data", ind["error"])
        self.assertFalse(fac["ok"])
        self.assertIn("No data", fac["error"])

    def test_portfolio_none_panel_is_structured_error(self):
        eng = QuantEngine()
        with mock.patch("quantkit.portfolio.fetch_price_panel", return_value=None):
            r = eng.run_portfolio(["600098", "600519"])
        self.assertFalse(r["ok"])
        self.assertIn("No price data", r["error"])


if __name__ == "__main__":
    unittest.main()

