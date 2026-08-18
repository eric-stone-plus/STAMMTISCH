"""Multi-market ticker resolve — shipped tui.symbols, no network."""

from __future__ import annotations

import unittest

from tui.symbols import normalize_symbol, resolve_query, search_payload


class NormalizeSymbolTest(unittest.TestCase):
    def test_cn_ashare(self):
        self.assertEqual(normalize_symbol("600098"), "600098.SS")
        self.assertEqual(normalize_symbol("000001"), "000001.SZ")
        self.assertEqual(normalize_symbol("300750"), "300750.SZ")
        self.assertEqual(normalize_symbol("830799"), "830799.BJ")

    def test_hk_jp_us(self):
        self.assertEqual(normalize_symbol("0700"), "0700.HK")
        self.assertEqual(normalize_symbol("700"), "0700.HK")
        self.assertEqual(normalize_symbol("7203"), "7203.T")
        self.assertEqual(normalize_symbol("AAPL"), "AAPL")
        self.assertEqual(normalize_symbol("0700.HK"), "0700.HK")
        self.assertEqual(normalize_symbol("600519.SS"), "600519.SS")
        # Official 5-digit HK (Pop Mart) → Yahoo 9992.HK
        self.assertEqual(normalize_symbol("09992"), "9992.HK")
        self.assertEqual(normalize_symbol("09992.HK"), "9992.HK")
        self.assertEqual(normalize_symbol("9992"), "9992.HK")

    def test_passthrough_short(self):
        self.assertEqual(normalize_symbol("6000"), "6000.T")


class ResolveQueryTest(unittest.TestCase):
    def test_ambiguous_six_digit_offers_kr(self):
        hits = resolve_query("005930")
        syms = [h["symbol"] for h in hits]
        self.assertIn("005930.SZ", syms)
        self.assertIn("005930.KS", syms)

    def test_name_index(self):
        apples = resolve_query("apple")
        self.assertTrue(any(h["symbol"] == "AAPL" for h in apples))
        tencent = resolve_query("tencent")
        self.assertTrue(any(h["symbol"] == "0700.HK" for h in tencent))
        samsung = resolve_query("samsung")
        self.assertTrue(any(h["symbol"] == "005930.KS" for h in samsung))
        toyota = resolve_query("toyota")
        self.assertTrue(any(h["symbol"] == "7203.T" for h in toyota))
        gz = resolve_query("600098")
        self.assertEqual(gz[0]["symbol"], "600098.SS")
        self.assertEqual(gz[0]["name"], "Guangzhou Development")
        self.assertNotIn("Moutai", gz[0]["name"])
        moutai = resolve_query("moutai")
        self.assertTrue(any(h["symbol"] == "600519.SS" for h in moutai))
        pop = resolve_query("09992")
        self.assertEqual(pop[0]["symbol"], "9992.HK")
        self.assertEqual(pop[0]["name"], "Pop Mart")
        self.assertTrue(any(h["symbol"] == "9992.HK" for h in resolve_query("pop mart")))

    def test_market_filter(self):
        hits = resolve_query("005930", market="KR")
        self.assertEqual([h["market"] for h in hits], ["KR"])
        self.assertEqual(hits[0]["symbol"], "005930.KS")

    def test_search_payload(self):
        p = search_payload("AAPL")
        self.assertTrue(p["ok"])
        self.assertTrue(p["hits"])
        self.assertEqual(p["hits"][0]["symbol"], "AAPL")
        self.assertNotIn("mic", p["hits"][0])

    def test_deterministic_suffixes_include_exact_mic(self):
        cases = {
            "600584.SS": "XSHG",
            "600584.SH": "XSHG",
            "300750.SZ": "XSHE",
            "830799.BJ": "XBEI",
            "0700.HK": "XHKG",
            "7203.T": "XTKS",
            "7203.TYO": "XTKS",
            "005930.KS": "XKRX",
            "247540.KQ": "XKRX",
        }
        for symbol, mic in cases.items():
            with self.subTest(symbol=symbol):
                hit = resolve_query(symbol)[0]
                self.assertEqual(mic, hit["mic"])

    def test_ambiguous_us_venue_is_never_guessed(self):
        for query in ("AAPL", "NVDA", "apple"):
            with self.subTest(query=query):
                for hit in resolve_query(query):
                    if hit["market"] == "US":
                        self.assertNotIn("mic", hit)


if __name__ == "__main__":
    unittest.main()
