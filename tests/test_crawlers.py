"""Crawler panel unit tests: source parsing/toggling, offline only."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tui.crawlers import _fit, parse_sources, toggle_source

CONF = """\
# SCOPE annotation line
domestic|fin-ashare|eastmoney|https://www.eastmoney.com/
domestic|fin-ashare|sina|https://finance.sina.com.cn/
#overseas|fin-hk|nikkei|https://www.nikkei.com/markets/
overseas|fin-us|cnbc|https://www.cnbc.com/markets/
"""


class ParseSourcesTest(unittest.TestCase):
    def test_parse_marks_enabled_and_toggleable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.conf"
            path.write_text(CONF, encoding="utf-8")
            entries = parse_sources(str(path))
        toggleable = [e for e in entries if e["toggleable"]]
        self.assertEqual(len(entries), 5)
        self.assertEqual(len(toggleable), 4)
        # The commented-out nikkei row stays a toggleable, disabled source.
        states = {(e["name"], e["enabled"]) for e in toggleable}
        self.assertIn(("eastmoney", True), states)
        self.assertIn(("nikkei", False), states)
        # The annotation line is carried but never switchable.
        annotation = next(e for e in entries if not e["toggleable"])
        self.assertIn("SCOPE", annotation["name"])

    def test_toggle_round_trip_preserves_other_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.conf"
            path.write_text(CONF, encoding="utf-8")
            # Disable cnbc (line index 4).
            self.assertTrue(toggle_source(4, str(path)))
            after = path.read_text(encoding="utf-8").splitlines()
            self.assertTrue(after[4].lstrip().startswith("#"))
            self.assertIn("cnbc", after[4])
            # Annotation and other rows are untouched.
            self.assertEqual(after[0], "# SCOPE annotation line")
            self.assertEqual(after[1].split("|")[2], "eastmoney")
            # Re-enable restores the exact original line.
            self.assertTrue(toggle_source(4, str(path)))
            self.assertEqual(
                path.read_text(encoding="utf-8"), CONF
            )

    def test_toggle_refuses_prose_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.conf"
            path.write_text(CONF, encoding="utf-8")
            self.assertFalse(toggle_source(0, str(path)), "annotation is not a source")
            self.assertFalse(toggle_source(99, str(path)))
            self.assertFalse(toggle_source(0, str(Path(tmp) / "missing.conf")))

    def test_fit_collapses_and_truncates(self) -> None:
        self.assertEqual(_fit("  a   b  ", 10), "a b")
        long = "x" * 80
        self.assertEqual(len(_fit(long, 20)), 20)
        self.assertTrue(_fit(long, 20).endswith("…"))


if __name__ == "__main__":
    unittest.main()
