"""Crawler panel unit tests: source parsing/toggling, offline only."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tui.crawlers import _fit, parse_sources, toggle_source

CONF = """\
# SCOPE annotation line
# 格式: phase|folder|name|url   （| 分隔，无空格；# 开头为注释）
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
        self.assertEqual(len(entries), 6)
        self.assertEqual(len(toggleable), 4)
        # The commented-out nikkei row stays a toggleable, disabled source.
        states = {(e["name"], e["enabled"]) for e in toggleable}
        self.assertIn(("eastmoney", True), states)
        self.assertIn(("nikkei", False), states)
        # Annotation prose — including the header comment whose format
        # description contains four |-separated words — is never a source.
        annotations = [e for e in entries if not e["toggleable"]]
        self.assertEqual(len(annotations), 2)
        self.assertIn("SCOPE", annotations[0]["name"])
        self.assertIn("格式", annotations[1]["name"])

    def test_toggle_round_trip_preserves_other_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.conf"
            path.write_text(CONF, encoding="utf-8")
            # Disable cnbc (line index 5).
            self.assertTrue(toggle_source(5, str(path)))
            after = path.read_text(encoding="utf-8").splitlines()
            self.assertTrue(after[5].lstrip().startswith("#"))
            self.assertIn("cnbc", after[5])
            # Annotation and other rows are untouched.
            self.assertEqual(after[0], "# SCOPE annotation line")
            self.assertIn("格式", after[1])
            self.assertEqual(after[2].split("|")[2], "eastmoney")
            # Re-enable restores the exact original line.
            self.assertTrue(toggle_source(5, str(path)))
            self.assertEqual(
                path.read_text(encoding="utf-8"), CONF
            )

    def test_toggle_refuses_prose_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.conf"
            path.write_text(CONF, encoding="utf-8")
            self.assertFalse(toggle_source(0, str(path)), "annotation is not a source")
            self.assertFalse(toggle_source(1, str(path)), "header prose is not a source")
            self.assertFalse(toggle_source(99, str(path)))
            self.assertFalse(toggle_source(0, str(Path(tmp) / "missing.conf")))

    def test_fit_collapses_and_truncates(self) -> None:
        self.assertEqual(_fit("  a   b  ", 10), "a b")
        long = "x" * 80
        self.assertEqual(len(_fit(long, 20)), 20)
        self.assertTrue(_fit(long, 20).endswith("…"))


if __name__ == "__main__":
    unittest.main()
