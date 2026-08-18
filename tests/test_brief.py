"""Daily-report compatibility readers and rendering; offline only."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tui.brief import format_brief, list_dates, load_daily, load_daily_path
from tui.symbols import normalize_symbol, resolve_query


def _report(day: str = "20260814") -> dict:
    return {
        "date": day,
        "model": "fixture",
        "brief": [{"text": "\u5e02\u573a\u6458\u8981", "sources": ["\u4f8b\u5b50\u8d22\u7ecf"]}],
        "markets": {
            "ashare": [{
                "title": "\u4e2d\u6587\u6e90\u6807\u9898\u539f\u6837\u4fdd\u7559",
                "url": "https://example.test/cn",
                "summary": "\u539f\u59cb\u8bed\u8a00\u6458\u8981",
                "sources": ["\u4f8b\u5b50\u8d22\u7ecf"],
            }],
            "hk": [],
            "us": [{
                "title": "English source title stays English",
                "url": "https://example.test/us",
                "summary": "Source-language deck",
                "sources": ["Example Wire"],
            }],
            "crypto": [],
        },
        "notes": ["\u4e2d\u6587\u65e5\u62a5\u5907\u6ce8"],
    }


class BriefIngestTest(unittest.TestCase):
    def test_explicit_report_path_preserves_source_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_json = root / "report.json"
            report_html = root / "report.html"
            report_json.write_text(json.dumps(_report(), ensure_ascii=False), encoding="utf-8")
            report_html.write_text("<!doctype html><html></html>", encoding="utf-8")

            doc = load_daily_path(
                report_json, html_path=report_html, expected_date="20260814"
            )

        self.assertTrue(doc["ok"], doc.get("error"))
        self.assertEqual(doc["date"], "20260814")
        self.assertIn("tape", doc, "sentiment remains a downstream compatibility field")
        text = format_brief(doc)
        self.assertIn("\u4e2d\u6587\u6e90\u6807\u9898\u539f\u6837\u4fdd\u7559", text)
        self.assertIn("English source title stays English", text)
        self.assertNotIn("MARKET SENTIMENT", text)
        self.assertNotIn("<html", text.lower())

    def test_explicit_report_rejects_date_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text(json.dumps(_report("20260813")), encoding="utf-8")
            doc = load_daily_path(path, expected_date="20260814")
        self.assertFalse(doc["ok"])
        self.assertIn("does not match", doc["error"])

    def test_legacy_tree_is_explicit_and_portable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "20260814" / "output"
            output.mkdir(parents=True)
            path = output / "fin-daily-20260814.refined.json"
            path.write_text(json.dumps(_report(), ensure_ascii=False), encoding="utf-8")
            (output / "fin-daily-20260814.html").write_text("<html></html>", encoding="utf-8")

            self.assertEqual(list_dates(root), ["20260814"])
            doc = load_daily("20260814", root)

        self.assertTrue(doc["ok"], doc.get("error"))
        self.assertEqual(doc["json_path"], str(path))

    def test_bad_or_missing_report_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("[]", encoding="utf-8")
            self.assertFalse(load_daily_path(path)["ok"])
            self.assertFalse(load_daily_path(path.with_name("missing.json"))["ok"])

    def test_600098_is_not_moutai(self) -> None:
        self.assertEqual(normalize_symbol("600098"), "600098.SS")
        hits = resolve_query("600098")
        self.assertEqual(hits[0]["name"], "Guangzhou Development")
        self.assertNotIn("Moutai", hits[0]["name"])

    def test_canonical_sibling_feeds_the_tape(self) -> None:
        """Sentiment scores the full canonical dataset, not the curated report."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "20260817T000000Z-fixtur03"
            run_dir.mkdir(parents=True)
            report = _report("20260817")
            report["markets"] = {
                "ashare": [{
                    "title": "精选层只有一条",
                    "url": "https://example.test/cn",
                    "summary": "精选摘要",
                    "sources": ["例子财经"],
                }],
                "hk": [], "us": [], "crypto": [],
            }
            report_json = run_dir / "fin-daily-20260817.json"
            report_json.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            records = [
                {"market": "ashare", "title": f"央行公告第{i}条", "summary": "逆回购",
                 "source": "eastmoney", "source_labels": ["东方财富"]}
                for i in range(12)
            ]
            (run_dir / "canonical-dataset.json").write_text(json.dumps({
                "date": "20260817", "records": records,
            }, ensure_ascii=False), encoding="utf-8")

            doc = load_daily_path(report_json, expected_date="20260817")

        self.assertTrue(doc["ok"], doc.get("error"))
        self.assertEqual(len(doc["markets"]["ashare"]), 1, "report layer stays curated")
        self.assertEqual(doc["tape"]["n"], 12, "tape scores every canonical record")

    def test_canonical_sibling_date_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "20260817T000000Z-fixtur04"
            run_dir.mkdir(parents=True)
            report_json = run_dir / "fin-daily-20260817.json"
            report_json.write_text(json.dumps(_report("20260817"), ensure_ascii=False), encoding="utf-8")
            (run_dir / "canonical-dataset.json").write_text(json.dumps({
                "date": "20260816",
                "records": [{"market": "us", "title": "Stale record", "summary": "x"}],
            }), encoding="utf-8")

            doc = load_daily_path(report_json, expected_date="20260817")

        self.assertTrue(doc["ok"], doc.get("error"))
        self.assertEqual(doc["tape"]["n"], 3, "falls back to the report layer")

    def test_format_brief_curates_long_market_lists(self) -> None:
        items = [
            {"title": f"标题{i:02d}", "url": f"https://example.test/{i}",
             "summary": "摘要", "source": "feed-a" if i % 2 else "feed-b"}
            for i in range(14)
        ]
        doc = {"ok": True, "date": "20260817", "model": "fixture", "brief": [],
               "markets": {"ashare": items, "hk": [], "us": [], "jp": [],
                           "kr": [], "sg": [], "crypto": []},
               "notes": []}
        text = format_brief(doc)
        self.assertIn("--- A-share (14) ---", text)
        self.assertIn("+4 more in the full dataset", text)
        self.assertEqual(text.count("  * 标题"), 10)
        # Source diversity: both feeds appear within the curated window.
        self.assertIn("标题00", text)
        self.assertIn("标题01", text)


if __name__ == "__main__":
    unittest.main()
