"""Datafeeds — registry fallback, cache, provider parsers, journal (offline)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tui.datafeeds import cache as dfcache
from tui.datafeeds import journal, registry, service
from tui.datafeeds.errors import FeedError
from tui.datafeeds.providers import binance, coingecko, stooq, tencent, yahoo


def _quote(symbol: str, source: str = "test") -> dict:
    return {"symbol": symbol, "name": symbol, "last": 10.0,
            "prev_close": 9.0, "open": 9.5, "high": 10.5, "low": 9.0,
            "volume": 1000.0, "time": "", "source": source}


class RegistryTest(unittest.TestCase):
    def setUp(self):
        registry.reset_stats()

    def test_with_fallback_returns_first_success(self):
        def boom():
            raise FeedError("a", "down")
        result = registry.with_fallback([
            ("a", boom),
            ("b", lambda: "ok"),
        ], on_fallback=lambda name, err: None)
        self.assertEqual(result, "ok")

    def test_with_fallback_raises_last_error(self):
        def boom(name):
            raise FeedError(name, "down")
        with self.assertRaises(FeedError):
            registry.with_fallback([
                ("a", lambda: boom("a")),
                ("b", lambda: boom("b")),
            ])
        stats = {entry.name: entry for entry in registry.all_stats()}
        self.assertEqual(stats["a"].failed, 1)
        self.assertEqual(stats["b"].failed, 1)

    def test_tracked_records_health(self):
        registry.tracked("p", lambda: 42)
        with self.assertRaises(ValueError):
            registry.tracked("p", lambda: (_ for _ in ()).throw(ValueError("no")))
        entry = {s.name: s for s in registry.all_stats()}["p"]
        self.assertEqual(entry.ok, 1)
        self.assertEqual(entry.failed, 1)
        self.assertIn("no", entry.last_error or "")
        self.assertIsNotNone(entry.avg_latency_ms)

    def test_empty_chain_raises(self):
        with self.assertRaises(FeedError):
            registry.with_fallback([])


class CacheTest(unittest.TestCase):
    def setUp(self):
        dfcache.reset_cache()
        dfcache.configure_disk_cache(None)

    def test_fresh_hit_skips_producer(self):
        calls = []
        def producer():
            calls.append(1)
            return "v"
        self.assertEqual(dfcache.cached("k", 60, producer), "v")
        self.assertEqual(dfcache.cached("k", 60, producer), "v")
        self.assertEqual(len(calls), 1)

    def test_expired_refetches(self):
        calls = []
        def producer():
            calls.append(len(calls))
            return calls[-1]
        self.assertEqual(dfcache.cached("k", -1, producer), 0)
        self.assertEqual(dfcache.cached("k", -1, producer), 1)

    def test_failure_falls_back_to_stale(self):
        dfcache.cached("k", 60, lambda: "first")
        def broken():
            raise FeedError("p", "down")
        self.assertEqual(dfcache.cached("k", -1, broken), "first")

    def test_failure_without_stale_raises(self):
        def broken():
            raise FeedError("p", "down")
        with self.assertRaises(FeedError):
            dfcache.cached("k", -1, broken)

    def test_disk_snapshot_used_when_memory_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            dfcache.configure_disk_cache(tmp)
            dfcache.cached("diskkey", 60, lambda: {"v": 1})
            dfcache.reset_cache()  # memory gone, disk snapshot remains
            def broken():
                raise FeedError("p", "down")
            self.assertEqual(dfcache.cached("diskkey", -1, broken), {"v": 1})
            self.assertEqual(dfcache.cache_stats()["disk_dir"], tmp)

    def test_disk_snapshot_respects_max_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            dfcache.configure_disk_cache(tmp)
            dfcache.cached("aged", 60, lambda: "old")
            dfcache.reset_cache()
            # Artificially age the snapshot beyond the window.
            path = next(Path(tmp).glob("*.json"))
            payload = json.loads(path.read_text())
            payload["ts"] = time.time() - 999999
            path.write_text(json.dumps(payload))
            with self.assertRaises(FeedError):
                dfcache.cached("aged", -1, lambda: (_ for _ in ()).throw(FeedError("p", "x")))


class TencentProviderTest(unittest.TestCase):
    def test_to_tencent_code(self):
        self.assertEqual(tencent.to_tencent_code("600519.SS"), "sh600519")
        self.assertEqual(tencent.to_tencent_code("000001.SZ"), "sz000001")
        self.assertEqual(tencent.to_tencent_code("0700.HK"), "hk00700")
        self.assertEqual(tencent.to_tencent_code("AAPL"), "usAAPL")
        self.assertEqual(tencent.to_tencent_code("HSI"), "hkHSI")
        self.assertIsNone(tencent.to_tencent_code("XXX.TO"))
        self.assertEqual(tencent.to_tencent_code("BTC-USD"), "usBTC-USD")

    def test_parse_batch(self):
        payload = (
            'v_sh600519="1~贵州茅台~600519~1500.00~1490.00~1495.00~'
            + "10000~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~"
            + "20260913150000~0~0~1510.00~1480.00~0~0~0~0~0~0~0~0~0~"
            + '0~0~0~"')
        parsed = tencent.parse_batch(payload)
        quote = parsed["sh600519"]
        self.assertEqual(quote["name"], "贵州茅台")
        self.assertEqual(quote["last"], 1500.0)
        self.assertEqual(quote["prev_close"], 1490.0)
        self.assertEqual(quote["high"], 1510.0)
        self.assertEqual(quote["low"], 1480.0)
        self.assertEqual(quote["source"], tencent.SOURCE)


class YahooProviderTest(unittest.TestCase):
    def test_to_yahoo_symbol(self):
        self.assertEqual(yahoo.to_yahoo_symbol("BRK.B"), "BRK-B")
        self.assertEqual(yahoo.to_yahoo_symbol("aapl"), "AAPL")

    def test_parse_chart_quote(self):
        payload = {"chart": {"result": [{"meta": {
            "symbol": "AAPL", "shortName": "Apple Inc.",
            "regularMarketPrice": 250.5, "previousClose": 248.0,
            "regularMarketDayHigh": 251.0, "regularMarketDayLow": 247.0,
            "regularMarketVolume": 55_000_000,
        }}], "error": None}}
        quote = yahoo.parse_chart_quote("AAPL", payload)
        self.assertEqual(quote["last"], 250.5)
        self.assertEqual(quote["prev_close"], 248.0)
        self.assertEqual(quote["name"], "Apple Inc.")
        self.assertEqual(quote["source"], yahoo.SOURCE)

    def test_parse_chart_quote_empty_raises(self):
        with self.assertRaises(FeedError):
            yahoo.parse_chart_quote("X", {"chart": {"result": [], "error": "bad"}})

    def test_parse_chart_candles_skips_null_rows(self):
        payload = {"chart": {"result": [{
            "timestamp": [1, 2, 3],
            "indicators": {"quote": [{
                "open": [10.0, None, 12.0],
                "high": [11.0, None, 13.0],
                "low": [9.0, None, 11.0],
                "close": [10.5, None, 12.5],
                "volume": [100, 200, 300],
            }]},
        }]}}
        candles = yahoo.parse_chart_candles(payload)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[-1]["close"], 12.5)
        self.assertEqual(candles[-1]["volume"], 300.0)


class StooqProviderTest(unittest.TestCase):
    def test_stooq_code(self):
        self.assertEqual(stooq.stooq_code("AAPL"), "aapl.us")
        self.assertEqual(stooq.stooq_code("0700.HK"), "00700.hk")
        self.assertEqual(stooq.stooq_code("600519.SS"), "600519.cn")

    def test_parse_csv(self):
        csv_text = (
            "Date,Open,High,Low,Close,Volume\n"
            "2026-09-11,100,105,99,104,12345\n"
            "2026-09-12,104,106,103,105,15000\n"
            '""\n'
        )
        candles = stooq.parse_csv(csv_text)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0]["time"], "2026-09-11")
        self.assertEqual(candles[1]["close"], 105.0)

    def test_parse_csv_bad_header_raises(self):
        with self.assertRaises(FeedError):
            stooq.parse_csv("No data available")


class CoingeckoProviderTest(unittest.TestCase):
    SAMPLE = [
        {"symbol": "btc", "name": "Bitcoin", "current_price": 60000.0,
         "price_change_percentage_24h": 1.5, "market_cap": 1_200_000_000_000,
         "total_volume": 30_000_000_000,
         "sparkline_in_7d": {"price": [1.0, 2.0, 3.0]}},
        {"symbol": "eth", "name": "Ethereum", "current_price": 3000.0,
         "price_change_percentage_24h": -2.0, "market_cap": 400_000_000_000,
         "total_volume": 15_000_000_000, "sparkline_in_7d": {"price": []}},
    ]

    def test_parse_markets(self):
        board = coingecko.parse_markets(self.SAMPLE)
        self.assertEqual(len(board["rows"]), 2)
        btc = board["rows"][0]
        self.assertEqual(btc["symbol"], "BTC")
        self.assertEqual(btc["chg_24h"], 1.5)
        self.assertEqual(board["btc_dominance"], 75.0)
        self.assertEqual(btc["source"], coingecko.SOURCE)

    def test_parse_markets_empty_raises(self):
        with self.assertRaises(FeedError):
            coingecko.parse_markets([])


class BinanceProviderTest(unittest.TestCase):
    def test_parse_klines(self):
        payload = [
            [1694000000000, "100", "110", "95", "105", "10", 0, 0, 0, 0, 0, 0],
            [1694100000000, "105", "115", "100", "112", "20", 0, 0, 0, 0, 0, 0],
        ]
        candles = binance.parse_klines(payload)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0]["close"], 105.0)
        self.assertEqual(candles[1]["high"], 115.0)

    def test_parse_ticker24h(self):
        payload = [{"symbol": "BTCUSDT", "lastPrice": "60000",
                    "prevClosePrice": "59000", "openPrice": "59000",
                    "highPrice": "61000", "lowPrice": "58500",
                    "volume": "1000"}]
        quotes = binance.parse_ticker24h(payload)
        self.assertEqual(quotes["BTCUSDT"]["last"], 60000.0)
        self.assertEqual(quotes["BTCUSDT"]["prev_close"], 59000.0)
        self.assertEqual(quotes["BTCUSDT"]["source"], binance.SOURCE)


class ServiceChainTest(unittest.TestCase):
    def setUp(self):
        registry.reset_stats()
        dfcache.reset_cache()
        dfcache.configure_disk_cache(None)

    def test_quotes_tencent_primary_yahoo_fills_missing(self):
        with mock.patch.object(tencent, "fetch_quotes",
                               return_value={"AAPL": _quote("AAPL", "t")}), \
             mock.patch.object(yahoo, "fetch_quotes",
                               return_value={"MSFT": _quote("MSFT", "y")}):
            result = service.quotes(["AAPL", "MSFT"])
        self.assertEqual(result["AAPL"]["source"], "t")
        self.assertEqual(result["MSFT"]["source"], "y")

    def test_quotes_tencent_failure_falls_back(self):
        with mock.patch.object(tencent, "fetch_quotes",
                               side_effect=FeedError("tencent", "down")), \
             mock.patch.object(yahoo, "fetch_quotes",
                               return_value={"AAPL": _quote("AAPL", "y")}):
            result = service.quotes(["AAPL"])
        self.assertEqual(result["AAPL"]["source"], "y")

    def test_quotes_all_down_returns_empty(self):
        with mock.patch.object(tencent, "fetch_quotes",
                               side_effect=FeedError("tencent", "down")), \
             mock.patch.object(yahoo, "fetch_quotes",
                               side_effect=FeedError("yahoo", "down")):
            self.assertEqual(service.quotes(["AAPL"]), {})

    def test_daily_candles_chain_and_cache(self):
        candles = [{"time": "2026-09-12", "open": 1, "high": 2, "low": 0.5,
                    "close": 1.5, "volume": 9}]
        with mock.patch.object(stooq, "fetch_candles", return_value=candles) as st:
            first = service.daily_candles("AAPL")
            second = service.daily_candles("AAPL")
        self.assertEqual(first, candles)
        self.assertEqual(second, candles)
        self.assertEqual(st.call_count, 1)  # second call served from cache

    def test_daily_candles_falls_back_to_yahoo(self):
        candles = [{"time": "2026-09-12", "open": 1, "high": 2, "low": 0.5,
                    "close": 1.5, "volume": 9}]
        with mock.patch.object(stooq, "fetch_candles",
                               side_effect=FeedError("stooq", "down")), \
             mock.patch.object(yahoo, "fetch_candles", return_value=candles):
            result = service.daily_candles("MSFT")
        self.assertEqual(result, candles)

    def test_crypto_board_coingecko_then_binance(self):
        board = {"rows": [{"symbol": "BTC", "name": "Bitcoin", "last": 1.0,
                           "chg_24h": 0.0, "market_cap": 1.0, "volume": 1.0,
                           "spark": [], "source": "coingecko"}],
                 "btc_dominance": 50.0}
        with mock.patch.object(coingecko, "fetch_board", return_value=board):
            result = service.crypto_board(limit=5)
        self.assertEqual(result["rows"][0]["symbol"], "BTC")
        dfcache.reset_cache()  # the first board result is cached under the same key
        with mock.patch.object(coingecko, "fetch_board",
                               side_effect=FeedError("coingecko", "down")), \
             mock.patch.object(binance, "fetch_quotes",
                               return_value={"BTCUSDT": _quote("BTCUSDT", "binance")}):
            result = service.crypto_board(limit=5)
        self.assertEqual(result["rows"][0]["last"], 10.0)
        self.assertEqual(result["rows"][0]["source"], "binance")


class JournalTest(unittest.TestCase):
    def test_append_and_read_tape(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = journal.append(tmp, {"AAPL": _quote("AAPL")})
            self.assertEqual(written, 1)
            journal.append(tmp, {"AAPL": _quote("AAPL", "y"),
                                 "MSFT": _quote("MSFT")})
            tape = journal.read_tape(tmp, "aapl")
            self.assertEqual(len(tape), 2)
            self.assertEqual(tape[0]["symbol"], "AAPL")
            self.assertEqual(tape[0]["last"], 10.0)
            self.assertEqual(tape[-1]["source"], "y")

    def test_append_skips_empty_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(journal.append(tmp, {"AAPL": {"last": None}}), 0)

    def test_append_survives_bad_root(self):
        with mock.patch.object(Path, "open", side_effect=OSError("nope")):
            self.assertEqual(journal.append("/nonexistent/x", {"A": _quote("A")}), 0)


class HttpProxyTest(unittest.TestCase):
    def setUp(self):
        from tui.datafeeds import http as dfhttp
        dfhttp.configure_data_proxy(None)

    def test_configure_sets_default_and_pins_tencent_direct(self):
        from tui.datafeeds import http as dfhttp
        dfhttp.configure_data_proxy("http://127.0.0.1:9")
        self.assertEqual(dfhttp.proxy_for("yahoo"), "http://127.0.0.1:9")
        self.assertEqual(dfhttp.proxy_for("binance"), "http://127.0.0.1:9")
        # The CN-side endpoint never rides the proxy.
        self.assertIsNone(dfhttp.proxy_for("tencent"))
        dfhttp.configure_data_proxy(None)
        self.assertIsNone(dfhttp.proxy_for("yahoo"))

    def test_get_text_routes_by_provider(self):
        from tui.datafeeds import http as dfhttp
        dfhttp.configure_data_proxy("http://127.0.0.1:9")
        opener = mock.Mock()
        opener.open.return_value = mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b"ok"
        with mock.patch.object(dfhttp.urllib.request, "build_opener",
                               return_value=opener) as build, \
             mock.patch.object(dfhttp.urllib.request, "urlopen") as direct:
            text = dfhttp.get_text("http://example.invalid/", provider="yahoo")
            self.assertEqual(text, "ok")
            build.assert_called_once()
            direct.assert_not_called()
            # The CN-side endpoint bypasses the proxy path entirely.
            dfhttp.get_text("http://example.invalid/", provider="tencent")
            build.assert_called_once()
            direct.assert_called_once()


class LivefeedDelegationTest(unittest.TestCase):
    def test_fetch_batch_delegates_to_service(self):
        from tui import livefeed
        with mock.patch.object(service, "quotes",
                               return_value={"AAPL": _quote("AAPL")}) as q:
            result = livefeed.fetch_batch(["AAPL"])
        self.assertEqual(result["AAPL"]["source"], "test")
        q.assert_called_once()

    def test_fetch_batch_failure_returns_empty(self):
        from tui import livefeed
        with mock.patch.object(service, "quotes", side_effect=RuntimeError("x")):
            self.assertEqual(livefeed.fetch_batch(["AAPL"]), {})

    def test_qt_source_exported(self):
        from tui import livefeed
        self.assertEqual(livefeed.QT_SOURCE, tencent.SOURCE)
        self.assertEqual(livefeed.QT_ENDPOINT, tencent.QT_ENDPOINT)


if __name__ == "__main__":
    unittest.main()


class ProxyFallbackTest(unittest.TestCase):
    def setUp(self):
        from tui.datafeeds import http as dfhttp
        dfhttp.configure_data_proxy(None)
        dfhttp.configure_proxy_fallback(None)

    def tearDown(self):
        from tui.datafeeds import http as dfhttp
        dfhttp.configure_data_proxy(None)
        dfhttp.configure_proxy_fallback(None)

    def test_dead_primary_retries_via_fallback(self):
        import urllib.error
        from tui.datafeeds import http as dfhttp

        dfhttp.configure_data_proxy("http://127.0.0.1:9")   # dead port
        dfhttp.configure_proxy_fallback("http://127.0.0.1:10")  # fake-live
        calls = []

        def fake_opener(proxy):
            calls.append(proxy)
            opener = mock.Mock()
            if proxy.endswith(":9"):
                opener.open.side_effect = urllib.error.URLError(
                    ConnectionRefusedError("refused"))
            else:
                resp = mock.MagicMock()
                resp.__enter__.return_value.read.return_value = b"ok"
                opener.open.return_value = resp
            return opener

        with mock.patch.object(dfhttp, "_opener", side_effect=fake_opener):
            text = dfhttp.get_text("http://example.invalid/", provider="yahoo")
        self.assertEqual(text, "ok")
        self.assertEqual(calls, ["http://127.0.0.1:9", "http://127.0.0.1:10"])

    def test_http_error_does_not_fall_back(self):
        import urllib.error
        from tui.datafeeds import http as dfhttp

        dfhttp.configure_data_proxy("http://127.0.0.1:9")
        dfhttp.configure_proxy_fallback("http://127.0.0.1:10")

        def fake_opener(proxy):
            opener = mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError(
                "url", 403, "forbidden", None, None)
            return opener

        with mock.patch.object(dfhttp, "_opener", side_effect=fake_opener):
            with self.assertRaises(urllib.error.HTTPError):
                dfhttp.get_text("http://example.invalid/", provider="yahoo")

    def test_no_fallback_configured_raises_primary_error(self):
        import urllib.error
        from tui.datafeeds import http as dfhttp

        dfhttp.configure_data_proxy("http://127.0.0.1:9")
        dfhttp.configure_proxy_fallback(None)
        with mock.patch.object(dfhttp, "_opener") as build:
            build.return_value.open.side_effect = urllib.error.URLError(
                ConnectionRefusedError("refused"))
            with self.assertRaises(urllib.error.URLError):
                dfhttp.get_text("http://example.invalid/", provider="yahoo")
            build.assert_called_once()
