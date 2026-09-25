"""Crypto engine bridge — adapter contract, topk portfolio (offline)."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
import numpy as np
from types import SimpleNamespace
from unittest import mock

from tui import cryptobacktest as cb
from services.engine import QuantEngine


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
                "    rows = [{'symbol': c,\n"
                "             'score': float(prices[c].iloc[-1])}\n"
                "            for c in prices.columns]\n"
                "    return pd.DataFrame(rows).set_index('symbol')\n"
                "def select_topk_dropout(scores, holdings, topk, n_drop, **kw):\n"
                "    ranked = list(scores.sort_values('score',\n"
                "                                   ascending=False).index)\n"
                "    sel = ranked[:topk]\n"
                "    w = pd.Series(1.0 / len(sel), index=sel)\n"
                "    return sel, w\n")
            (pkg / "portfolio.py").write_text(
                "import pandas as pd\n"
                "from types import SimpleNamespace\n"
                "def fetch_price_panel(symbols, *, start=None, data_dir=None, **kw):\n"
                "    idx = pd.date_range('2024-01-01', periods=30, freq='D')\n"
                "    return pd.DataFrame({s: [i + 1] * 30\n"
                "                         for i, s in enumerate(symbols)}, index=idx)\n"
                "def run_portfolio(prices, weights, **kw):\n"
                "    stats = {'avg_turnover': 0.1, 'avg_gross_exposure': 0.9}\n"
                "    return SimpleNamespace(total_return=0.12, cagr=0.1,\n"
                "        sharpe=0.9, max_drawdown=-0.05, win_rate=0.6, trades=4,\n"
                "        stats=stats)\n")

            def _unstub():
                import sys

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


class BatchScreenerTest(unittest.TestCase):
    def test_crypto_screen_offline_and_persist(self):
        from services import batch_screener as bs

        cfg = _config("paper")
        cfg.data_proxy_url = ""
        cfg.egress_proxy_url = ""
        ticker = [{"symbol": "BTCUSDT", "quoteVolume": "50"},
                  {"symbol": "ETHUSDT", "quoteVolume": "40"},
                  {"symbol": "LSKUSDT", "quoteVolume": "3"}]
        # Zigzag closes: each 6-bar cycle drives RSI(2) below 10 and back
        # above 60, so the sweep actually produces round trips. (The old
        # flat-then-ramp fixture produced zero trades and silently pinned
        # NaN tier medians — exactly the failure mode the ok:False guard
        # now refuses.)
        closes = np.tile([100.0, 99.0, 88.0, 90.0, 95.0, 99.0], 25)
        klines = [[0, 0, 0, 0, float(c), 0] for c in closes]
        with mock.patch.object(bs.dfhttp, "get_json",
                               side_effect=lambda url, **kw: (
                                   ticker if "ticker" in url else klines)), \
             mock.patch.object(bs, "configure_data_proxy"), \
             mock.patch.object(bs, "configure_proxy_fallback"):
            out = bs.crypto_screen(cfg, min_volume=2, limit=10, workers=2)
        self.assertEqual(out["universe"], 3)
        self.assertEqual(out["evaluated"], 3)
        self.assertIn("0.05%", out["fee_tiers"])
        for tier in out["fee_tiers"].values():
            self.assertTrue(np.isfinite(tier["median_net"]), tier)
            self.assertTrue(np.isfinite(tier["positive_share"]), tier)
        with tempfile.TemporaryDirectory() as tmp:
            path = bs.persist(tmp, "crypto", out)
            self.assertTrue(path.is_file())
            self.assertIn("evaluated", json.loads(path.read_text()))


class TradeSafetyTest(unittest.TestCase):
    def _bars(self, wick: bool):
        base = [{"open": 100 + i, "high": 105 + i, "low": 99 + i,
                 "close": 102 + i} for i in range(20)]
        if wick:
            base[-1] = {"open": 120, "high": 121, "low": 60,
                        "close": 119}  # 60-point lower wick on ~5 ATR
        return base

    def test_wick_guard_blocks_spike_bar(self):
        from tui.tradesafety import wick_ok

        ok, why = wick_ok(self._bars(wick=False))
        self.assertTrue(ok)
        ok, why = wick_ok(self._bars(wick=True))
        self.assertFalse(ok)
        self.assertIn("stop-hunt", why)

    def test_martingale_caps(self):
        from tui.tradesafety import martingale_qty

        self.assertEqual(martingale_qty(0, 1.0, 0.0, 10000, 100), 1.0)
        self.assertAlmostEqual(martingale_qty(1, 1.0, 0.05, 10000, 100), 1.5)
        self.assertEqual(martingale_qty(3, 1.0, 0.0, 10000, 100), 0.0)  # max steps
        # Total-exposure cap breach -> zero, never a bigger size.
        self.assertEqual(martingale_qty(1, 1.0, 0.14, 10000, 100), 0.0)

    def test_kill_switch_reads_journal(self):
        from tui.tradesafety import daily_loss_ok

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mr-loop.jsonl"
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            path.write_text("\n".join(json.dumps(r) for r in [
                {"ts": now, "kind": "exit", "pnl": -150.0},
                {"ts": now, "kind": "exit", "pnl": -40.0},
            ]))
            ok, realized = daily_loss_ok(path, 10000, 0.02)  # cap -200
            self.assertTrue(ok)
            self.assertAlmostEqual(realized, -190.0)
            path.write_text("\n".join(json.dumps(r) for r in [
                {"ts": now, "kind": "exit", "pnl": -150.0},
                {"ts": now, "kind": "exit", "pnl": -60.0},
                {"ts": now, "kind": "exit", "pnl": -10.0},
            ]))
            ok, realized = daily_loss_ok(path, 10000, 0.02)
            self.assertFalse(ok)  # -220 breaches the -200 cap

    def test_stop_market_spec(self):
        from tui.tradesafety import stop_market_spec

        spec = stop_market_spec("ETHUSDT", 0.05, 2500.0, 0.05)
        self.assertEqual(spec["side"], "SELL")
        self.assertEqual(spec["algoType"], "CONDITIONAL")
        self.assertEqual(spec["triggerprice"], 2375.0)
        self.assertEqual(spec["quantity"], 0.05)
        self.assertEqual(spec["workingType"], "MARK_PRICE")  # wick-resistant


if "time" not in dir():
    import time
