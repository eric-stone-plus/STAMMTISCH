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


class EgressRotationTest(unittest.TestCase):
    """Rate-limited fetches rotate once; anything else stays direct."""

    PROXY = "http://proxy.example:8080"
    SWITCH = "switch --for-site {host} --yes"

    def _engine(self, **kwargs) -> QuantEngine:
        defaults = dict(egress_proxy_url=self.PROXY,
                        egress_switch_cmd=self.SWITCH)
        defaults.update(kwargs)
        return QuantEngine(**defaults)

    def test_rate_limit_rotates_once_and_retries(self):
        eng = self._engine()
        frame = pd.DataFrame({"close": [1.0]})
        calls = {"n": 0}

        def fake_fetch(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("Too Many Requests")
            return frame

        with mock.patch("quantkit.data.fetch_ohlcv", side_effect=fake_fetch), \
             mock.patch.object(eng, "_egress_apply") as apply, \
             mock.patch("subprocess.run") as run:
            out = eng._fetch_ohlcv("AAPL", "auto", "2020-01-01")
        self.assertIs(out, frame)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(run.call_count, 2)
        argv = [c.args[0] for c in run.call_args_list]
        self.assertEqual(argv[0], ["switch", "--for-site",
                                   "query1.finance.yahoo.com", "--yes"])
        self.assertEqual(argv[1], ["switch", "--for-site",
                                   "query2.finance.yahoo.com", "--yes"])
        apply.assert_called_once_with(self.PROXY)

    def test_non_rate_limit_does_not_rotate(self):
        eng = self._engine()
        with mock.patch("quantkit.data.fetch_ohlcv",
                        side_effect=RuntimeError("no data")), \
             mock.patch("subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "no data"):
                eng._fetch_ohlcv("AAPL", "auto", "2020-01-01")
        run.assert_not_called()

    def test_no_switch_cmd_does_not_rotate(self):
        eng = QuantEngine(egress_proxy_url=self.PROXY)
        with mock.patch("quantkit.data.fetch_ohlcv",
                        side_effect=RuntimeError("HTTP 429")), \
             mock.patch("subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "429"):
                eng._fetch_ohlcv("AAPL", "auto", "2020-01-01")
        run.assert_not_called()

    def test_already_active_does_not_rotate(self):
        eng = self._engine()
        eng._egress_active = True
        with mock.patch("quantkit.data.fetch_ohlcv",
                        side_effect=RuntimeError("rate limited")), \
             mock.patch("subprocess.run") as run:
            with self.assertRaisesRegex(RuntimeError, "rate limited"):
                eng._fetch_ohlcv("AAPL", "auto", "2020-01-01")
        run.assert_not_called()

    def test_fetch_data_routes_through_wrapper(self):
        eng = QuantEngine()
        with mock.patch.object(eng, "_fetch_ohlcv", return_value=None) as wrapped:
            result = eng.fetch_data("AAPL")
        wrapped.assert_called_once()
        self.assertEqual(wrapped.call_args.kwargs["market"], "auto")
        self.assertFalse(result["ok"])
        self.assertIn("No data", result["error"])

    def test_backtest_indicators_factors_route_through_wrapper(self):
        eng = QuantEngine()
        with mock.patch.object(eng, "_fetch_ohlcv", return_value=None) as wrapped:
            bt = eng.run_backtest("600098")
            ind = eng.compute_indicators("600098")
            fac = eng.build_factors("600098")
        self.assertEqual(wrapped.call_count, 3)
        self.assertFalse(bt["ok"])
        self.assertFalse(ind["ok"])
        self.assertFalse(fac["ok"])


if __name__ == "__main__":
    unittest.main()

