"""ml_signal tool handler: argument validation and train/load fallback."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

from unittest import mock

import pandas as pd

from tui.tools import _ml_signal


class _Engine:
    """Minimal QuantEngine stand-in backed by an in-memory frame map."""

    def __init__(self, data: dict[str, pd.DataFrame] | None = None):
        self._data = data or {}

    def fetch_data(self, symbol, market="auto", start=None):
        if symbol in self._data:
            return {"ok": True, "df": self._data[symbol]}
        return {"ok": False, "error": "no data"}


class _FakePipeline:
    """MLPipeline stub recording how the handler trained it."""

    load_ok = False
    instances: list["_FakePipeline"] = []

    def __init__(self, model_dir=None):
        self.model_dir = model_dir
        self.best_model = None
        self.train_calls: list[tuple] = []
        self.cross_calls: list[tuple] = []
        _FakePipeline.instances.append(self)

    def load_best(self):
        return _FakePipeline.load_ok

    def train(self, df, label_col=None):
        self.train_calls.append((df, label_col))

    def train_cross_sectional(self, data, label_col=None):
        self.cross_calls.append((dict(data), label_col))

    def predict(self, df):
        return pd.Series([0.1] * len(df))


def _frame(rows: int = 10) -> pd.DataFrame:
    return pd.DataFrame({"close": [float(i) for i in range(rows)]})


def _fake_quantkit():
    quantkit = types.ModuleType("quantkit")
    ml_pipeline = types.ModuleType("quantkit.ml_pipeline")
    ml_pipeline.MLPipeline = _FakePipeline
    quantkit.ml_pipeline = ml_pipeline
    return mock.patch.dict(
        sys.modules, {"quantkit": quantkit, "quantkit.ml_pipeline": ml_pipeline})


class MLSignalValidationTest(unittest.TestCase):
    def test_years_not_a_number(self):
        out = _ml_signal(_Engine(), {"symbols": ["AAPL"], "years": "abc"})
        self.assertEqual(out, "error: years must be a number")

    def test_years_below_one_rejected(self):
        for bad in (0, 0.5, -2):
            out = _ml_signal(_Engine(), {"symbols": ["AAPL"], "years": bad})
            self.assertEqual(out, "error: years must be >= 1")

    def test_symbols_required(self):
        self.assertEqual(_ml_signal(_Engine(), {}),
                         "error: symbols must be a non-empty array")
        self.assertEqual(_ml_signal(_Engine(), {"symbols": []}),
                         "error: symbols must be a non-empty array")

    def test_no_data_for_any_symbol(self):
        with _fake_quantkit():
            out = _ml_signal(_Engine(), {"symbols": ["AAPL"]})
        self.assertEqual(out, "error: no data available for any symbol")

    def test_ml_pipeline_unavailable(self):
        # sys.modules entry of None makes the guarded import raise
        # ImportError; the sibling fallback is pinned absent too, so the
        # degradation never depends on which trees exist on the host.
        with mock.patch.dict(sys.modules, {"quantkit.ml_pipeline": None}), \
             mock.patch("tui.tools._load_sibling_ml_pipeline",
                        return_value=None):
            out = _ml_signal(_Engine({"AAPL": _frame()}), {"symbols": ["AAPL"]})
        self.assertEqual(out, "error: ml_pipeline module not available")

    def test_sibling_fallback_serves_evolved_tree_without_ml_pipeline(self):
        # The operator's quantkit (evolved fork) is importable but ships
        # no ml_pipeline: the package import fails and the handler must
        # fall back to the sibling load and still train. (A None entry is
        # the deterministic stand-in for "submodule absent from the
        # installed tree" — an editable meta-finder would otherwise
        # resolve the submodule by name behind a fake parent.)
        _FakePipeline.instances = []
        sibling = types.ModuleType("_stammtisch_galahad_ml_pipeline")
        sibling.MLPipeline = _FakePipeline
        with mock.patch.dict(sys.modules, {"quantkit.ml_pipeline": None}), \
             mock.patch("tui.tools._load_sibling_ml_pipeline",
                        return_value=sibling):
            out = _ml_signal(_Engine({"AAPL": _frame()}), {"symbols": ["AAPL"]})
        self.assertNotIn("error:", out)
        self.assertEqual(len(_FakePipeline.instances), 1)


    def test_runtime_dependency_gap_degrades_to_error_string(self):
        # A pickled model needing an absent booster (ModuleNotFoundError
        # out of load_best) must become a clear error string, never an
        # unhandled exception in the chat driver.
        class _BrokenPipeline(_FakePipeline):
            def load_best(self):
                raise ModuleNotFoundError("No module named 'lightgbm'")

        sibling = types.ModuleType("_stammtisch_galahad_ml_pipeline")
        sibling.MLPipeline = _BrokenPipeline
        with mock.patch.dict(sys.modules, {"quantkit.ml_pipeline": None}), \
             mock.patch("tui.tools._load_sibling_ml_pipeline",
                        return_value=sibling):
            out = _ml_signal(_Engine({"AAPL": _frame()}), {"symbols": ["AAPL"]})
        self.assertTrue(out.startswith("error: ml_pipeline failed:"), out)
        self.assertIn("lightgbm", out)

    def test_broken_sibling_degrades_to_error_string(self):
        # A present-but-broken sibling raises out of the loader (degrade
        # loudly); the handler must convert it to a clear error string
        # carrying the reason, never an unhandled exception in the chat
        # driver and never the vague "module not available".
        with mock.patch.dict(sys.modules, {"quantkit.ml_pipeline": None}), \
             mock.patch("tui.tools._load_sibling_ml_pipeline",
                        side_effect=RuntimeError(
                            "sibling ml_pipeline failed to load: boom")):
            out = _ml_signal(_Engine({"AAPL": _frame()}), {"symbols": ["AAPL"]})
        self.assertTrue(
            out.startswith("error: ml_pipeline sibling load failed:"), out)
        self.assertIn("boom", out)


class SiblingLoaderTest(unittest.TestCase):
    """The file-based sibling loader: offline, host-independent."""

    def _write_sibling(self, root, source: str):
        pkg = root / "quantkit"
        pkg.mkdir(parents=True)
        (pkg / "ml_pipeline.py").write_text(source, encoding="utf-8")

    def test_loads_module_from_file(self):
        from tui.tools import _load_sibling_ml_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            self._write_sibling(Path(tmp), "class MLPipeline:\n    pass\n")
            module = _load_sibling_ml_pipeline(galahad_root=Path(tmp))
        self.assertIsNotNone(module)
        self.assertTrue(hasattr(module, "MLPipeline"))

    def test_missing_sibling_returns_none(self):
        from tui.tools import _load_sibling_ml_pipeline

        self.assertIsNone(
            _load_sibling_ml_pipeline(galahad_root=Path("/nonexistent-sibling")))

    def test_broken_module_degrades_loudly(self):
        # FIXES.md B-3/E-1: a present-but-broken sibling must never
        # masquerade as an absent one — the loader raises with the
        # reason, and the half-initialized module is popped from
        # sys.modules so no stale partial stays reachable by name.
        from tui.tools import _load_sibling_ml_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            self._write_sibling(Path(tmp), "raise RuntimeError('broken')\n")
            with self.assertRaises(RuntimeError) as ctx:
                _load_sibling_ml_pipeline(galahad_root=Path(tmp))
        self.assertIn("broken", str(ctx.exception))
        self.assertNotIn("_stammtisch_galahad_ml_pipeline", sys.modules)

    def test_loads_dataclass_module_with_postponed_annotations(self):
        # The real GALAHAD ml_pipeline.py is ``from __future__ import
        # annotations`` + @dataclass: on Python 3.14 dataclasses._is_type
        # resolves the defining module through sys.modules DURING class
        # construction, so the loader must register the module before
        # exec_module. This source is the exact shape that silently
        # returned None (AttributeError on the unregistered module)
        # before the registration fix.
        from tui.tools import _load_sibling_ml_pipeline

        source = (
            "from __future__ import annotations\n"
            "from dataclasses import dataclass, field\n"
            "@dataclass\n"
            "class ModelResult:\n"
            "    symbol: str\n"
            "    preds: list[float] = field(default_factory=list)\n"
            "class MLPipeline:\n"
            "    pass\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            self._write_sibling(Path(tmp), source)
            module = _load_sibling_ml_pipeline(galahad_root=Path(tmp))
        self.assertIsNotNone(module)
        self.assertTrue(hasattr(module, "MLPipeline"))
        self.assertEqual(module.ModelResult("AAPL").symbol, "AAPL")


@unittest.skipUnless(
    os.environ.get("STAMMTISCH_IT") == "1",
    "set STAMMTISCH_IT=1 to run the real-sibling loader integration test",
)
class RealSiblingLoaderTest(unittest.TestCase):
    """Integration pin against the ACTUAL GALAHAD sibling checkout.

    The offline SiblingLoaderTest execs synthetic sources; this one
    loads the real ml_pipeline.py (dataclasses, quantkit.indicators
    import against the installed tree) — the shape that exposed the
    missing sys.modules registration.
    """

    def test_real_galahad_ml_pipeline_loads(self):
        from tui.tools import _load_sibling_ml_pipeline

        module = _load_sibling_ml_pipeline()
        self.assertIsNotNone(module)
        self.assertTrue(hasattr(module, "MLPipeline"))


class MLSignalPipelineTest(unittest.TestCase):
    def setUp(self):
        _FakePipeline.instances = []
        _FakePipeline.load_ok = False

    def test_single_symbol_trains_on_that_frame(self):
        df = _frame()
        with _fake_quantkit():
            out = _ml_signal(_Engine({"AAPL": df}), {"symbols": ["AAPL"]})
        pipe = _FakePipeline.instances[0]
        # The single-symbol fallback trains directly on the fetched frame;
        # a loadable model would skip training entirely.
        self.assertEqual(len(pipe.train_calls), 1)
        self.assertIs(pipe.train_calls[0][0], df)
        self.assertEqual(pipe.train_calls[0][1], "fwd_ret_5")
        self.assertEqual(pipe.cross_calls, [])
        rows = json.loads(out)
        self.assertEqual(rows[0]["symbol"], "AAPL")
        self.assertEqual(rows[0]["model"], "none")

    def test_cross_sectional_with_three_symbols(self):
        data = {"AAPL": _frame(), "MSFT": _frame(), "QQQ": _frame()}
        with _fake_quantkit():
            _ml_signal(_Engine(data), {"symbols": list(data)})
        pipe = _FakePipeline.instances[0]
        self.assertEqual(pipe.train_calls, [])
        self.assertEqual(len(pipe.cross_calls), 1)
        self.assertEqual(set(pipe.cross_calls[0][0]), set(data))

    def test_loadable_model_skips_training(self):
        _FakePipeline.load_ok = True
        with _fake_quantkit():
            _ml_signal(_Engine({"AAPL": _frame()}), {"symbols": ["AAPL"]})
        pipe = _FakePipeline.instances[0]
        self.assertEqual(pipe.train_calls, [])
        self.assertEqual(pipe.cross_calls, [])


if __name__ == "__main__":
    unittest.main()
