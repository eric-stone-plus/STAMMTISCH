"""Crypto engine bridge — adapter contract, topk portfolio (offline)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tui import cryptobacktest as cb
from tui.engine import QuantEngine


def _config(cmd: str = "", quantkit_path: str = ""):
    cfg = SimpleNamespace()
    data = {"crypto_backtest_cmd": cmd, "quantkit_path": quantkit_path}
    cfg.get = lambda key, default=None: data.get(key, default)
    return cfg


_VALID = {
    "schema": cb.SCHEMA,
    "generated_at": "2026-09-13T00:00:00+08:00",
    "config": {"symbol": "BTC/USDT", "timeframe": "1d"},
    "results": [{
        "strategy": "bollinger", "total_return_pct": 12.5,
        "max_drawdown_pct": -8.1, "sharpe_ratio": 1.1, "sortino_ratio": 1.4,
        "calmar_ratio": 0.9, "exposure_pct": 30.0, "win_rate": 55.0,
        "total_trades": 12, "profit_factor": 1.6,
    }],
}


class BuildArgsTest(unittest.TestCase):
    def test_unconfigured_refuses(self):
        with self.assertRaises(cb.CryptoBacktestError):
            cb.build_args(_config(), symbol="BTC/USDT", timeframe="1d",
                          start="2024-01-01")

    def test_tokenized_without_shell(self):
        args = cb.build_args(_config("python3 /opt/engines/cb.py"),
                             symbol="BTC/USDT", timeframe="4h",
                             start="2024-01-01", end="2026-01-01",
                             strategy="sma")
        self.assertEqual(args[:2], ["python3", "/opt/engines/cb.py"])
        self.assertEqual(args[2:], [
            "--symbol", "BTC/USDT", "--timeframe", "4h",
            "--start", "2024-01-01", "--strategy", "sma", "--json",
            "--end", "2026-01-01",
        ])


class ValidatePayloadTest(unittest.TestCase):
    def test_valid_payload_passes(self):
        self.assertEqual(cb.validate_payload(_VALID)["results"][0]["strategy"],
                         "bollinger")

    def test_failures(self):
        with self.assertRaises(cb.CryptoBacktestError):
            cb.validate_payload(["not", "a", "dict"])
        bad_schema = dict(_VALID, schema="crypto.backtest.v0")
        with self.assertRaises(cb.CryptoBacktestError):
            cb.validate_payload(bad_schema)
        empty = dict(_VALID, results=[])
        with self.assertRaises(cb.CryptoBacktestError):
            cb.validate_payload(empty)
        non_numeric = json.loads(json.dumps(_VALID))
        non_numeric["results"][0]["sharpe_ratio"] = "1.1"
        with self.assertRaises(cb.CryptoBacktestError):
            cb.validate_payload(non_numeric)


class RunTest(unittest.TestCase):
    def test_happy_path_with_fake_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "engine.py"
            script.write_text(
                "import json, sys\n"
                "assert '--json' in sys.argv\n"
                "print(json.dumps(" + json.dumps(_VALID) + "))\n")
            cfg = _config(f"{sys.executable} {script}")
            payload = cb.run(cfg, symbol="BTC/USDT", timeframe="1d",
                             start="2024-01-01")
            self.assertEqual(payload["schema"], cb.SCHEMA)

    def test_nonzero_exit_surfaces_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "bad.py"
            script.write_text("import sys\nprint('boom', file=sys.stderr)\n"
                              "raise SystemExit(3)\n")
            cfg = _config(f"{sys.executable} {script}")
            with self.assertRaises(cb.CryptoBacktestError) as ctx:
                cb.run(cfg, symbol="BTC/USDT", timeframe="1d",
                       start="2024-01-01")
            self.assertIn("exited 3", str(ctx.exception))
            self.assertIn("boom", str(ctx.exception))


class TopkPortfolioTest(unittest.TestCase):
    def test_capability_gap_is_actionable(self):
        """The installed public quantkit has no selection module."""
        engine = QuantEngine(data_dir="/tmp/qk-nonexistent")
        result = engine.run_topk_portfolio(["AAPL", "MSFT"])
        self.assertFalse(result["ok"])
        self.assertIn("quantkit_path", result["error"])

    def test_topk_runs_against_stub_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "quantkit"
            pkg.mkdir()
            (pkg / "__init__.py").write_text("")
            (pkg / "selection.py").write_text(
                "import pandas as pd\n"
                "def score_universe(prices, *, mode='momentum', asof=None,\n"
                "                   mom_lookback=60, **kw):\n"
                "    last = prices.index[-1]\n"
                "    rows = [{'symbol': c, 'score': float(prices[c].iloc[-1])}\n"
                "            for c in prices.columns]\n"
                "    return pd.DataFrame(rows).set_index('symbol')\n"
                "def select_topk_dropout(scores, holdings, topk, n_drop, **kw):\n"
                "    ranked = list(scores.sort_values('score', ascending=False).index)\n"
                "    sel = ranked[:topk]\n"
                "    return sel, pd.Series(1.0 / len(sel), index=sel)\n")
            (pkg / "portfolio.py").write_text(
                "import pandas as pd\n"
                "from types import SimpleNamespace\n"
                "def fetch_price_panel(symbols, *, start=None, data_dir=None, **kw):\n"
                "    import pandas as pd\n"
                "    idx = pd.date_range('2024-01-01', periods=30, freq='D')\n"
                "    return pd.DataFrame({s: [i + 1] * 30 for i, s in enumerate(symbols)},\n"
                "                        index=idx)\n"
                "def run_portfolio(prices, weights, **kw):\n"
                "    stats = {'avg_turnover': 0.1, 'avg_gross_exposure': 0.9}\n"
                "    return SimpleNamespace(total_return=0.12, cagr=0.1,\n"
                "        sharpe=0.9, max_drawdown=-0.05, win_rate=0.6, trades=4,\n"
                "        stats=stats)\n")

            # A prior real-quantkit import must not shadow the stub tree.
            sys.modules.pop("quantkit", None)
            for name in [m for m in list(sys.modules) if m.startswith("quantkit.")]:
                sys.modules.pop(name, None)
            # Teardown: the stub tree must not shadow the real quantkit
            # for the rest of the suite (sys.path + module cache).
            def _unstub():
                while tmp in sys.path:
                    sys.path.remove(tmp)
                for name in [m for m in list(sys.modules)
                             if m == "quantkit" or m.startswith("quantkit.")]:
                    sys.modules.pop(name, None)
            self.addCleanup(_unstub)
            engine = QuantEngine(data_dir="/tmp/qk-nonexistent",
                                 quantkit_path=tmp)
            self.assertIsNotNone(engine.quantkit_tree)
            result = engine.run_topk_portfolio(
                ["AAPL", "MSFT", "GOOG"], topk=2, n_drop=1, rebalance="M")
            self.assertTrue(result["ok"], result.get("error"))
            self.assertEqual(result["summary"].n_assets, 2)
            # Stub panel prices rise with column order: GOOG scores
            # highest, then MSFT — topk 2 takes exactly those.
            self.assertEqual(result["final_holdings"], ["GOOG", "MSFT"])

    def test_bad_tree_degrades_with_recorded_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = QuantEngine(data_dir="/tmp/qk-x", quantkit_path=tmp)
            self.assertIsNone(engine.quantkit_tree)
            self.assertIn("no quantkit package", engine.quantkit_error or "")


if __name__ == "__main__":
    unittest.main()


class QuantkitPathDegradationTest(unittest.TestCase):
    def test_bad_path_degrades_instead_of_crashing_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = QuantEngine(data_dir="/tmp/qk-x",
                                 quantkit_path=str(Path(tmp) / "nowhere"))
            self.assertIsNone(engine.quantkit_tree)
            self.assertIn("no quantkit package", engine.quantkit_error or "")
            # The workstation keeps working on the installed quantkit.
            self.assertIsInstance(engine.available, bool)

    def test_from_config_survives_bad_path(self):
        cfg = SimpleNamespace()
        cfg.data_dir = "/tmp/qk-x"
        cfg.data_proxy_url = ""
        cfg.egress_proxy_url = ""
        cfg.egress_switch_cmd = ""
        cfg.quantkit_path = "/nonexistent/tree"
        engine = QuantEngine.from_config(cfg)
        self.assertIsNone(engine.quantkit_tree)
        self.assertIsNotNone(engine.quantkit_error)


class TimingStatusTest(unittest.TestCase):
    def test_hold_and_cash_states_offline(self):
        from tui.timing import status

        def fake_bars(prices):
            import datetime

            start = datetime.datetime(2025, 1, 1)
            return [{"t": (start + datetime.timedelta(days=i)).isoformat() + "Z",
                     "c": price} for i, price in enumerate(prices)]

        cfg = _config("paper")
        cfg.data_proxy_url = ""
        cfg.egress_proxy_url = ""
        broker = mock.Mock()
        broker.daily_bars.side_effect = lambda symbol: fake_bars(
            list(range(100, 320)) if symbol == "SPY" else list(range(320, 100, -1)))
        with mock.patch("tui.timing.AlpacaBroker", return_value=broker), \
             mock.patch("tui.timing.configure_data_proxy"), \
             mock.patch("tui.timing.configure_proxy_fallback"):
            out = status(cfg, ("SPY", "QQQ"))
        rows = {row["symbol"]: row for row in out["rows"]}
        self.assertEqual(rows["SPY"]["state"], "HOLD")    # rising series
        self.assertEqual(rows["QQQ"]["state"], "CASH")    # falling series
