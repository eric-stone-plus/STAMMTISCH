"""ml_signal tool handler: argument validation and train/load fallback."""

from __future__ import annotations

import json
import sys
import types
import unittest

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
        # ImportError, simulating a host without the optional quantkit.
        with mock.patch.dict(sys.modules, {"quantkit.ml_pipeline": None}):
            out = _ml_signal(_Engine({"AAPL": _frame()}), {"symbols": ["AAPL"]})
        self.assertEqual(out, "error: ml_pipeline module not available")


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
