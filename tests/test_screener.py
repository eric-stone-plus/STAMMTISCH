"""Screener tests: engine parity, quality floors, zones, snapshot dedupe."""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tui.engine import QuantEngine
from tui.screener import _dual_ma_metrics, screen_market


def _trending_close(n: int = 500, seed: int = 7) -> pd.Series:
    t = np.arange(n, dtype=float)
    rng = np.random.default_rng(seed)
    wave = 0.05 * np.sin(t / 23.0) + 0.02 * np.sin(t / 7.0)
    close = 100.0 * np.exp(0.0012 * t + wave + 0.004 * rng.standard_normal(n))
    index = pd.bdate_range("2024-01-02", periods=n)
    return pd.Series(close, index=index, name="close")


def _write_cache(tmp: Path, symbol: str, close: pd.Series, end: str = "na") -> None:
    cache = tmp / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame({"close": close})
    frame["open"] = frame["high"] = frame["low"] = close
    frame["volume"] = 1000.0
    path = cache / f"fake_auto_{symbol}_1d_2024-01-01_{end}.parquet"
    frame.to_parquet(path)


class MirrorParityTest(unittest.TestCase):
    def test_matches_engine_to_rounding(self):
        """The screen's numbers must be the engine's numbers — anything
        else lets the pre-rank and the verification disagree."""
        from quantkit.backtest import dual_ma_signal, run_long_only

        close = _trending_close()
        result = run_long_only(close, dual_ma_signal(close), cost_tier="low")
        # quantkit charges half the round-trip tier per one-sided unit:
        # "low" 0.2% round trip = 10 bps per side.
        mirror = _dual_ma_metrics(close.to_numpy(float), 10.0)
        self.assertAlmostEqual(mirror["tr"], result.total_return, places=8)
        self.assertAlmostEqual(mirror["sharpe"], result.sharpe, places=2)
        self.assertEqual(mirror["trades"], result.trades)


class ScreenMarketTest(unittest.TestCase):
    def test_floors_zones_and_ranking(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            _write_cache(tmp, "600001.SS", _trending_close(seed=7))
            _write_cache(tmp, "600002.SS", _trending_close(seed=8))
            # steady climb: MA20 stays above MA50, one round trip →
            # fails the trades floor
            steady = pd.Series(
                100.0 * np.exp(0.001 * np.arange(500)),
                index=pd.bdate_range("2024-01-02", periods=500))
            _write_cache(tmp, "600003.SS", steady)
            _write_cache(tmp, "ZZZZ", _trending_close(seed=9))
            # unknown exchange suffix → OTHER, never returned
            _write_cache(tmp, "WEIRD.XX", _trending_close(seed=10))

            engine = QuantEngine(data_dir=str(tmp))
            zones = screen_market(engine, per_zone=5)

            self.assertIn("A-SHARE", zones)
            self.assertIn("US", zones)
            self.assertNotIn("OTHER", zones)
            a_syms = [r["symbol"] for r in zones["A-SHARE"]]
            self.assertNotIn("600003.SS", a_syms, "one-trade series must not pass")
            self.assertIn("600001.SS", a_syms)
            trs = [r["tr"] for r in zones["A-SHARE"]]
            self.assertEqual(trs, sorted(trs, reverse=True))

    def test_duplicate_snapshots_resolve_to_latest(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            old = _trending_close(seed=1)
            recent = old.iloc[:450].copy()
            recent.iloc[-1] *= 1.02
            _write_cache(tmp, "600009.SS", old, end="20250101")
            _write_cache(tmp, "600009.SS", recent, end="20260101")

            engine = QuantEngine(data_dir=str(tmp))
            zones = screen_market(engine)
            rows = zones.get("A-SHARE", [])
            self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
