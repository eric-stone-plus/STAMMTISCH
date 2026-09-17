"""Tape-watch scorer — hype must not set the stance."""

from __future__ import annotations

import unittest

from tui.tape import build_tape, classify, format_tape, score_item


class ClassifyTest(unittest.TestCase):
    def test_hard_policy_and_print(self):
        self.assertEqual(
            classify("央行发布二季度货币政策执行报告，开展10000亿元逆回购"),
            "hard",
        )
        self.assertEqual(
            classify("Wholesale prices were flat in July, below 0.2% increase"),
            "hard",
        )

    def test_hype_limit_up_theme(self):
        self.assertEqual(
            classify("算力租赁概念爆发，多股涨停"),
            "hype",
        )

    def test_rumor_hearsay(self):
        self.assertEqual(
            classify("据报内地将对境外保单收益征税20%"),
            "rumor",
        )


class StanceTest(unittest.TestCase):
    def test_all_hype_is_watch_not_constructive(self):
        markets = {
            "ashare": [
                {"title": "算力租赁概念爆发，多股涨停", "summary": "游资热炒"},
                {"title": "AI龙头战法起飞狂飙", "summary": "必看"},
                {"title": "题材股暴涨神话", "summary": "跟风"},
                {"title": "游资热炒宇宙级行情", "summary": "涨停"},
            ],
            "hk": [],
            "us": [],
            "crypto": [],
        }
        tape = build_tape([], markets, [])
        self.assertEqual(tape["stance"], "watch")
        self.assertGreaterEqual(tape["hype_share"], 0.35)
        self.assertTrue(any("do not chase" in c for c in tape["caveats"]))
        self.assertLess(abs(score_item("算力租赁概念爆发，多股涨停")["score"]), 0.2)

    def test_hard_prints_are_not_capped_as_hype(self):
        markets = {
            "ashare": [
                {"title": "央行发布二季度货币政策执行报告，开展10000亿元逆回购",
                 "summary": "适度宽松"},
                {"title": "中芯国际第二季度销售收入30.06亿美元，纯利同比升261.7%",
                 "summary": "财报"},
            ],
            "us": [
                {"title": "Wholesale prices were flat in July, below 0.2% increase",
                 "summary": "PPI"},
                {"title": "Fed officials split after the CPI print",
                 "summary": "policy"},
            ],
            "hk": [],
            "crypto": [],
        }
        tape = build_tape([], markets, ["Stocktwits 抓取失败（http=500）。"])
        self.assertLess(tape["hype_share"], 0.35)
        self.assertFalse(any("do not chase" in c for c in tape["caveats"]))
        self.assertTrue(any(e["kind"] == "hard" for e in tape["evidence"]))
        self.assertTrue(any("Stocktwits" in c for c in tape["caveats"]))

    def test_format_tape_mentions_stance(self):
        tape = build_tape(
            [{"text": "央行发布二季度货币政策执行报告"}],
            {"ashare": [{"title": "央行发布二季度货币政策执行报告，10000亿元逆回购"}]},
            [],
        )
        text = format_tape(tape)
        self.assertIn("MARKET SENTIMENT", text)
        self.assertIn("STANCE=", text)
        self.assertNotIn("lightweight-charts", text.lower())


class NameAttachTest(unittest.TestCase):
    def test_theme_heat_does_not_stick_to_unrelated_ticker(self):
        from tui.tape import symbol_tape
        markets = {
            "ashare": [{"title": "算力租赁概念爆发，多股涨停", "summary": "游资"}],
            "hk": [{"title": "中芯国际第二季度销售收入30.06亿美元", "summary": "财报"}],
            "us": [],
            "crypto": [],
        }
        gz = symbol_tape("600098.SS", [], markets)
        self.assertEqual(gz["n"], 0)
        self.assertEqual(gz["stance"], "watch")
        smic = symbol_tape("0981.HK", [], markets)
        self.assertGreaterEqual(smic["n"], 1)
        self.assertTrue(any("中芯" in h["text"] for h in smic["hits"]))
        self.assertEqual(smic["hits"][0]["kind"], "hard")

    def test_name_hype_stays_watch(self):
        from tui.tape import symbol_tape
        markets = {
            "us": [
                {"title": "NVDA soars to the moon meme FOMO parabolic", "summary": "hype"},
                {"title": "Nvidia skyrocket rally 涨停爆发", "summary": "hype"},
            ],
            "ashare": [], "hk": [], "crypto": [],
        }
        st = symbol_tape("NVDA", [], markets)
        self.assertGreaterEqual(st["n"], 1)
        self.assertEqual(st["stance"], "watch")

    def test_desk_sentiment_has_market_and_name(self):
        from tui.tape import desk_sentiment
        doc = {
            "tape": {"stance": "constructive", "score": 0.3, "hype_share": 0.1,
                     "n": 4, "by_market": {}, "caveats": [], "evidence": []},
            "brief": [{"text": "腾讯控股获南向资金净买入56亿港元"}],
            "markets": {"hk": [{"title": "腾讯控股获南向资金净买入56亿港元"}]},
        }
        text = desk_sentiment(doc, "0700.HK")
        self.assertIn("MARKET SENTIMENT", text)
        self.assertIn("0700.HK", text)
        self.assertIn("腾讯", text)
        market_only = desk_sentiment(doc, None)
        self.assertIn("MARKET SENTIMENT", market_only)
        self.assertNotIn("SYMBOL ", market_only)
        self.assertNotIn("600098", market_only)
        empty_name = desk_sentiment(doc, "600098.SS")
        self.assertIn("SYMBOL 600098.SS", empty_name)
        self.assertIn("No matching headline", empty_name)


class RecordsModeTest(unittest.TestCase):
    """The full canonical dataset, not the curated report, drives the tape."""

    def _records(self):
        return [
            {"market": "ashare", "title": "央行开展5000亿元逆回购，净投放加码",
             "summary": "货币政策宽松超预期", "source": "eastmoney",
             "source_labels": ["东方财富"]},
            {"market": "ashare", "title": "白酒龙头财报净利润增长20%",
             "summary": "业绩超预期", "source": "sina", "source_labels": ["新浪财经"]},
            {"market": "hk", "title": "恒生指数回落，科技股下跌",
             "summary": "南向资金净卖出", "source": "aastocks",
             "source_labels": ["阿斯达克"]},
            {"market": "jp", "title": "Nikkei rises on earnings beat",
             "summary": "guidance higher", "source": "nikkei",
             "source_labels": ["Nikkei"]},
            {"market": "ashare", "title": "算力概念爆发，多股涨停",
             "summary": "游资热炒", "source": "10jqka", "source_labels": ["同花顺"]},
        ]

    def test_records_mode_scores_every_record(self):
        report_markets = {"ashare": [{"title": "白酒龙头财报净利润增长20%"}],
                          "hk": [], "us": [], "crypto": []}
        tape = build_tape([], report_markets, [], records=self._records())
        # The jp record is out of the operator's A/H/US scope and is dropped.
        self.assertEqual(tape["n"], 4)
        self.assertEqual(tape["by_market"]["ashare"]["n"], 3)
        self.assertEqual(tape["by_market"]["hk"]["n"], 1)
        self.assertNotIn("jp", tape["by_market"])

    def test_records_mode_breakdowns(self):
        tape = build_tape([], {}, [], records=self._records())
        kinds = tape["kinds"]
        self.assertGreaterEqual(kinds.get("hard", 0), 2)
        self.assertEqual(kinds.get("hype", 0), 1)
        sources = {row["source"] for row in tape["by_source"]}
        self.assertIn("东方财富", sources)
        bull_texts = [d["text"] for d in tape["drivers"]["bull"]]
        self.assertTrue(any("逆回购" in t for t in bull_texts))
        bear_texts = [d["text"] for d in tape["drivers"]["bear"]]
        self.assertTrue(any("回落" in t for t in bear_texts))

    def test_format_tape_renders_breakdowns(self):
        tape = build_tape([], {}, [], records=self._records())
        text = format_tape(tape)
        self.assertIn("KINDS", text)
        self.assertIn("HARD=", text)
        self.assertIn("SRC", text)
        self.assertIn("东方财富", text)
        self.assertIn("+ [HARD]", text)
        self.assertIn("- [", text)

    def test_drivers_are_not_repeated_as_evidence(self):
        tape = build_tape([], {}, [], records=self._records())
        text = format_tape(tape)
        driver_texts = [
            d["text"] for d in tape["drivers"]["bull"] + tape["drivers"]["bear"]
        ]
        self.assertTrue(driver_texts)
        for driver_text in driver_texts:
            self.assertEqual(text.count(driver_text), 1, driver_text[:40])

    def test_report_layer_mode_is_unchanged(self):
        markets = {"ashare": [{"title": "央行开展5000亿元逆回购", "summary": "宽松"}],
                   "hk": [], "us": [], "crypto": []}
        tape = build_tape([], markets, [])
        self.assertEqual(tape["n"], 1)
        self.assertIn("kinds", tape)
        self.assertIn("drivers", tape)


if __name__ == "__main__":
    unittest.main()
