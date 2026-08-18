"""Drive the shipped chart math (tui/static/chart_math.js) via Node.

The browser page loads this file; these tests require the same file.
They do not re-implement resample / legend / pane formulas.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CHART_MATH = REPO / "tui" / "static" / "chart_math.js"
WEB_CHART = REPO / "tui" / "web_chart.html"


def _call(fn: str, *args):
    """Invoke a function exported by the shipped chart_math.js."""
    script = (
        "const m = require(%s);\n"
        "const args = %s;\n"
        "const out = m[%s](...args);\n"
        "process.stdout.write(JSON.stringify(out));\n"
    ) % (json.dumps(str(CHART_MATH)), json.dumps(args), json.dumps(fn))
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            "chart_math.js %s failed:\n%s\n%s" % (fn, proc.stdout, proc.stderr)
        )
    return json.loads(proc.stdout)


def _prop(name: str):
    script = (
        "const m = require(%s);\n"
        "process.stdout.write(JSON.stringify(m[%s]));\n"
    ) % (json.dumps(str(CHART_MATH)), json.dumps(name))
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


class ChartMathShippedTest(unittest.TestCase):
    def test_page_loads_shipped_math_and_has_no_placeholder(self):
        html = WEB_CHART.read_text()
        self.assertIn("/static/chart_math.js", html)
        self.assertIn("/static/lightweight-charts.standalone.production.js", html)
        self.assertIn('id="symbol"', html)
        self.assertNotIn("placeholder=", html)
        self.assertNotIn("barsBeforeCurrent", html)
        self.assertNotIn("param.barData", html)
        self.assertIn("ChartMath.legendSnapshot", html)
        self.assertIn("ChartMath.forecastPoints", html)
        self.assertIn("ChartMath.resample", html)
        self.assertIn("ChartMath.pathSymbol", html)
        self.assertIn("ChartMath.priceScaleApply", html)
        self.assertIn("ChartMath.LoadSeq", html)
        self.assertIn("ChartMath.historyRecord", html)
        self.assertIn("ChartMath.historyRemove", html)
        self.assertIn("ChartMath.sanitizeCandles", html)
        self.assertIn("HISTORY", html)
        self.assertNotIn(">WATCH<", html)
        self.assertNotIn('x="7.5"', html)
        self.assertNotIn('<rect x="2"', html)
        self.assertNotIn('<rect x="13"', html)
        self.assertNotIn("brand-mark", html)
        brand = html[html.find('id="brand"'):html.find('id="search-box"')]
        self.assertIn("STAMMTISCH", brand)
        self.assertNotIn("<svg", brand)
        self.assertIn("goHome", html)
        self.assertIn("page-home", html)
        self.assertIn("page-chart", html)
        self.assertIn("launch-grid", html)
        self.assertIn('class="launch"', html)
        self.assertNotIn("body.page-home #desk", html)
        desk = html[html.find('id="desk"'):html.find('id="chart-wrap"')]
        for fixture in (
            "600098.SS", "600519.SS", "300750.SZ", "9992.HK", "0700.HK",
            "AAPL", "NVDA", "7203.T", "005930.KS",
        ):
            self.assertNotIn('data-sym="%s"' % fixture, desk)
        self.assertIn("id=\"search-box\"", html)
        self.assertIn('id="help"', html)
        self.assertIn("9992.HK", html)
        self.assertIn("600098.SS", html)
        self.assertIn("Guangzhou Development", html)
        self.assertIn("/api/search", html)
        self.assertIn("KRONOS", html)
        self.assertIn("pinPaneScales", html)
        self.assertIn("subscribeVisibleLogicalRangeChange", html)
        self.assertIn("PANE.compare.scaleId", html)
        self.assertNotIn("location.pathname.replace", html)
        self.assertNotRegex(html, r'<input[^>]*id="symbol"[^>]*value=')
        self.assertIn('id="chart-wrap"', html)
        self.assertIn("labelVisible: false", html)
        self.assertNotRegex(html, r"[\u4e00-\u9fff]")
        math = CHART_MATH.read_text()
        self.assertNotRegex(math, r"[\u4e00-\u9fff]")

    def test_legend_change_vs_previous_close_not_this_open(self):
        # First close 10, second open 20 / close 11. Change vs prev close
        # is +10%; change vs this open would be −45%. Volume lives on the
        # histogram series (value: 777), not on the candle.
        bars = [
            {"time": "2026-01-01", "open": 9.0, "high": 11.0, "low": 8.0,
             "close": 10.0, "volume": 1},
            {"time": "2026-01-02", "open": 20.0, "high": 21.0, "low": 10.5,
             "close": 11.0, "volume": 2},
        ]
        candle = {"open": 20.0, "high": 21.0, "low": 10.5, "close": 11.0}
        vol_point = {"value": 777.0}
        snap = _call("legendSnapshot", bars, "2026-01-02", vol_point, candle)
        self.assertEqual(snap["volume"], 777.0)
        self.assertNotEqual(snap["volume"], candle.get("volume"))
        prev_close = bars[0]["close"]
        this_open = candle["open"]
        vs_prev = (candle["close"] - prev_close) / prev_close * 100
        vs_open = (candle["close"] - this_open) / this_open * 100
        self.assertAlmostEqual(snap["changePct"], vs_prev)
        self.assertNotAlmostEqual(snap["changePct"], vs_open)
        text = _call("formatLegend", snap)
        self.assertNotIn("NaN", text)
        self.assertIn("777", text)
        self.assertIn(" O ", text)
        self.assertIn(" H ", text)
        self.assertIn(" L ", text)
        self.assertIn(" C ", text)
        self.assertIn(" V ", text)
        self.assertNotRegex(text, r"[\u4e00-\u9fff]")

    def test_legend_volume_missing_is_not_nan(self):
        bars = [
            {"time": "2026-01-01", "open": 1, "high": 1, "low": 1, "close": 1},
        ]
        candle = {"open": 1, "high": 1, "low": 1, "close": 1}
        snap = _call("legendSnapshot", bars, "2026-01-01", None, candle)
        self.assertIsNone(snap["volume"])
        text = _call("formatLegend", snap)
        self.assertNotIn("NaN", text)
        self.assertIn(" V -", text)
        self.assertNotRegex(text, r"[\u4e00-\u9fff]")

    def test_resample_d_w_m_envelopes_and_unique_times(self):
        # Two weeks, two months: Mon 2026-01-05 .. and Mon 2026-02-02.
        bars = [
            {"time": "2026-01-05", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
            {"time": "2026-01-06", "open": 11, "high": 15, "low": 8, "close": 13, "volume": 200},
            {"time": "2026-01-07", "open": 13, "high": 14, "low": 10, "close": 12, "volume": 50},
            {"time": "2026-02-02", "open": 20, "high": 22, "low": 19, "close": 21, "volume": 10},
            {"time": "2026-02-03", "open": 21, "high": 25, "low": 18, "close": 19, "volume": 30},
        ]
        daily = _call("resample", bars, "D")
        self.assertEqual([b["time"] for b in daily], [b["time"] for b in bars])
        self.assertTrue(_call("timesAreMonotonicUnique", daily))

        weekly = _call("resample", bars, "W")
        self.assertTrue(_call("timesAreMonotonicUnique", weekly))
        self.assertEqual(len({b["time"] for b in weekly}), len(weekly))
        jan = [b for b in bars if b["time"].startswith("2026-01")]
        feb = [b for b in bars if b["time"].startswith("2026-02")]
        self.assertEqual(len(weekly), 2)
        self.assertEqual(weekly[0]["open"], jan[0]["open"])
        self.assertEqual(weekly[0]["close"], jan[-1]["close"])
        self.assertEqual(weekly[0]["high"], max(b["high"] for b in jan))
        self.assertEqual(weekly[0]["low"], min(b["low"] for b in jan))
        self.assertEqual(weekly[0]["volume"], sum(b["volume"] for b in jan))
        self.assertEqual(weekly[1]["open"], feb[0]["open"])
        self.assertEqual(weekly[1]["close"], feb[-1]["close"])
        self.assertEqual(weekly[1]["high"], max(b["high"] for b in feb))
        self.assertEqual(weekly[1]["low"], min(b["low"] for b in feb))

        monthly = _call("resample", bars, "M")
        self.assertTrue(_call("timesAreMonotonicUnique", monthly))
        self.assertEqual(len({b["time"] for b in monthly}), len(monthly))
        self.assertEqual(len(monthly), 2)
        self.assertEqual(monthly[0]["high"], max(b["high"] for b in jan))
        self.assertEqual(monthly[0]["low"], min(b["low"] for b in jan))
        self.assertEqual(monthly[1]["high"], max(b["high"] for b in feb))

    def test_pane_layout_regions_do_not_overlap(self):
        layout = _prop("PANE_LAYOUT")
        self.assertTrue(_call("paneLayoutOk", layout))
        self.assertNotEqual(layout["compare"]["scaleId"], layout["volume"]["scaleId"])
        self.assertNotEqual(layout["compare"]["scaleId"], layout["macd"]["scaleId"])
        price = _call("occupiedRange", layout["price"])
        volume = _call("occupiedRange", layout["volume"])
        macd = _call("occupiedRange", layout["macd"])
        compare = _call("occupiedRange", layout["compare"])
        self.assertFalse(_call("rangesOverlap", price, volume))
        self.assertFalse(_call("rangesOverlap", price, macd))
        self.assertFalse(_call("rangesOverlap", volume, macd))
        self.assertFalse(_call("rangesOverlap", compare, volume))
        self.assertFalse(_call("rangesOverlap", compare, macd))
        # Overlay scales must stay invisible so zoom/pan cannot promote
        # them onto the left/right axes (the "sides vanish" failure).
        vol_opts = _call("priceScaleApply", layout["volume"])
        macd_opts = _call("priceScaleApply", layout["macd"])
        price_opts = _call("priceScaleApply", layout["price"])
        cmp_opts = _call("priceScaleApply", layout["compare"])
        self.assertFalse(vol_opts["visible"])
        self.assertFalse(macd_opts["visible"])
        self.assertFalse(cmp_opts["visible"])
        self.assertTrue(price_opts["visible"])
        self.assertFalse(vol_opts["alignLabels"])
        self.assertTrue(price_opts["autoScale"])
        self.assertEqual(vol_opts["scaleMargins"]["top"], layout["volume"]["top"])
        self.assertEqual(price_opts["scaleMargins"]["bottom"], layout["price"]["bottom"])

    def test_forecast_drops_missing_dates(self):
        values = [1.0, 2.0, 3.0, 4.0]
        dates = ["2026-03-01", "", None, "2026-03-04"]
        pts = _call("forecastPoints", values, dates)
        times = [p["time"] for p in pts]
        self.assertNotIn("", times)
        self.assertNotIn(None, times)
        self.assertEqual(times[0], "2026-03-01")
        self.assertEqual(times[-1], "2026-03-04")
        self.assertEqual(len(pts), 4)
        # No last bar and no dates at all → nothing to plot.
        self.assertEqual(_call("forecastPoints", values, None), [])
        self.assertEqual(_call("forecastPoints", values, []), [])

    def test_forecast_synthesizes_dates_from_last_bar(self):
        # Kronos sometimes returns values without dates. The overlay must
        # still plot, stitched to the last candle, never with time "".
        last = {"time": "2026-03-06", "close": 10.0}  # Friday
        pts = _call("forecastPoints", [11.0, 12.0], [], last)
        times = [p["time"] for p in pts]
        self.assertNotIn("", times)
        self.assertEqual(times[0], "2026-03-06")
        self.assertEqual(pts[0]["value"], 10.0)
        self.assertEqual(times[1], "2026-03-09")  # skip weekend
        self.assertEqual(pts[1]["value"], 11.0)
        self.assertEqual(len(pts), 3)

    def test_load_seq_stale_after_second_next(self):
        script = (
            "const m = require(%s);\n"
            "const g = new m.LoadSeq();\n"
            "const a = g.next();\n"
            "const b = g.next();\n"
            "process.stdout.write(JSON.stringify({\n"
            "  a: a, b: b, aCurrent: g.isCurrent(a), bCurrent: g.isCurrent(b)\n"
            "}));\n"
        ) % (__import__("json").dumps(str(CHART_MATH)))
        import json
        import subprocess
        proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        out = json.loads(proc.stdout)
        self.assertEqual(out["a"], 1)
        self.assertEqual(out["b"], 2)
        self.assertFalse(out["aCurrent"])
        self.assertTrue(out["bCurrent"])

    def test_path_symbol_only_from_chart_capture(self):
        # The old replace(/^\/chart\/?/, "") turned pathname "/" into "/"
        # and called load("/"). Only /chart/<non-empty> preloads.
        self.assertEqual(_call("pathSymbol", "/"), "")
        self.assertEqual(_call("pathSymbol", "/chart"), "")
        self.assertEqual(_call("pathSymbol", "/chart/"), "")
        self.assertEqual(_call("pathSymbol", "/chart//"), "")
        self.assertEqual(_call("pathSymbol", "/nope"), "")
        self.assertEqual(_call("pathSymbol", "/chart/600098"), "600098")
        self.assertEqual(_call("pathSymbol", "/chart/600098.SS"), "600098.SS")
        self.assertEqual(_call("pathSymbol", "/chart/600098,600519"), "600098,600519")
        self.assertEqual(_call("pathSymbol", "/chart/600098%2CSS"), "600098,SS")
        # The broken replace would have returned "/" for GET /.
        self.assertNotEqual(_call("pathSymbol", "/"), "/")

    def test_symbol_identity_preserves_exact_mic_without_alias_guessing(self):
        exact = _call("symbolIdentity", "H30184@XSHG")
        self.assertEqual(
            {"symbol": "H30184", "mic": "XSHG", "token": "H30184@XSHG"},
            exact,
        )
        self.assertEqual("XSHG", _call("micOf", "600584.SS"))
        self.assertEqual("XSHE", _call("micOf", "300750.SZ"))
        self.assertEqual("", _call("micOf", "AAPL"))
        self.assertEqual(
            {"symbol": "931743", "mic": "", "token": "931743"},
            _call("symbolIdentity", "931743"),
        )
        self.assertIsNone(_call("symbolIdentity", "H30184@cn"))
        self.assertIsNone(_call("symbolIdentity", "H30184@XSHG@XSHE"))
        self.assertIsNone(
            _call("symbolIdentity", {"symbol": "H30184@XSHG", "mic": "XSHE"})
        )

    def test_every_verified_leg_is_visible_and_copyable(self):
        def provenance(symbol: str, mic: str, bars: str, output: str):
            identity = {
                "symbol": symbol,
                "market": mic,
                "interval": "1d",
                "calendar": mic,
                "currency": "CNY",
                "price_basis": "raw",
                "volume_unit": "shares",
            }
            reference = {
                "schema": "stammtisch.validated-bars-reference.v1",
                "bars_sha256": bars,
                "output_sha256": output,
                "identity": identity,
                "calendar": {"name": mic, "sessions_sha256": "c" * 64},
            }
            return {
                "data_mode": "validated",
                "bars_sha256": bars,
                "output_sha256": output,
                "identity": identity,
                "reference": reference,
            }

        primary = _call(
            "validatedProvenanceLeg",
            "primary",
            "H30184",
            "XSHG",
            provenance("H30184", "XSHG", "a" * 64, "b" * 64),
        )
        compare = _call(
            "validatedProvenanceLeg",
            "compare",
            "931743",
            "XSHG",
            provenance("931743", "XSHG", "d" * 64, "e" * 64),
        )
        self.assertIsNotNone(primary)
        self.assertIsNotNone(compare)
        copied = _call(
            "provenanceText", [primary, compare], ["compare:931743@XSHG:return_from_first_close"]
        )
        for expected in (
            "verified_legs=2",
            "leg[0].symbol=H30184",
            "leg[1].symbol=931743",
            "leg[0].mic=XSHG",
            "leg[0].bars_sha256=" + "a" * 64,
            "leg[1].output_sha256=" + "e" * 64,
            "leg[1].calendar=",
            "drawn_transforms=compare:931743@XSHG:return_from_first_close",
        ):
            self.assertIn(expected, copied)
        bad = provenance("931743", "XSHG", "d" * 64, "e" * 64)
        bad["reference"]["bars_sha256"] = "f" * 64
        self.assertIsNone(
            _call("validatedProvenanceLeg", "compare", "931743", "XSHG", bad)
        )

    def test_history_record_remove_persist(self):
        empty = _call("historyParse", None)
        self.assertEqual(empty, [])
        after_a = _call("historyRecord", empty, {"symbol": "AAA", "market": "US", "name": "A"})
        after_b = _call("historyRecord", after_a, {"symbol": "BBB", "market": "US", "name": "B"})
        self.assertEqual([h["symbol"] for h in after_b], ["BBB", "AAA"])
        self.assertEqual(after_b[0]["symbol"], "BBB")
        removed = _call("historyRemove", after_b, "AAA")
        self.assertEqual([h["symbol"] for h in removed], ["BBB"])
        dumped = _call("historyDump", removed)
        reloaded = _call("historyParse", dumped)
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["symbol"], "BBB")
        self.assertEqual(reloaded[0]["name"], "B")

    def test_sanitize_candles_unique_ascending_finite(self):
        dirty = [
            {"time": "2026-01-03", "open": 3, "high": 4, "low": 2, "close": 3.5, "volume": 10},
            {"time": "2026-01-01", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 5},
            {"time": "2026-01-01", "open": 1.5, "high": 2.5, "low": 0.4, "close": 2, "volume": 7},
            {"time": "2026-01-02", "open": float("nan"), "high": 2, "low": 1, "close": 1, "volume": 1},
            {"time": "", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        ]
        out = _call("sanitizeCandles", dirty)
        times = [b["time"] for b in out]
        self.assertEqual(times, sorted(set(times)))
        self.assertTrue(_call("timesAreMonotonicUnique", out))
        self.assertEqual(times, ["2026-01-01", "2026-01-03"])
        first = out[0]
        self.assertEqual(first["open"], 1)
        self.assertEqual(first["high"], 2.5)
        self.assertEqual(first["low"], 0.4)
        self.assertEqual(first["close"], 2)
        self.assertEqual(first["volume"], 12)
        for bar in out:
            for key in ("open", "high", "low", "close", "volume"):
                self.assertTrue(isinstance(bar[key], (int, float)))
                self.assertEqual(bar[key], bar[key])  # not NaN


if __name__ == "__main__":
    unittest.main()
