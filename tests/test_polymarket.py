"""Polymarket tape — formatting and fail-closed proxy behavior (offline)."""

from __future__ import annotations

import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import build_opener
from unittest import mock

from tui import polymarket as pm


SAMPLE = {
    "id": "559651",
    "question": "Will BTC close above 100k?",
    "slug": "btc-100k",
    "outcomePrices": '["0.0445", "0.9555"]',
    "outcomes": '["Yes", "No"]',
    "volume24hr": 22747.236865,
    "volumeNum": 11954396.74,
    "endDateIso": "2026-12-31",
    "feeSchedule": {"exponent": 1, "rate": 0.04, "takerOnly": True},
    "category": "Crypto",
}


class _LocalHTTPHandler(BaseHTTPRequestHandler):
    """Tiny target/proxy fixture; a proxy request has an absolute-form path."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib server API
        server = self.server
        server.paths.append(self.path)
        request_number = len(server.paths)
        if getattr(server, "block_first", False) and request_number == 1:
            server.entered.set()
            server.release.wait(timeout=5)
        body = server.response_body
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass


class _LocalHTTPServer:
    def __init__(self, response_body: bytes, *, block_first: bool = False):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _LocalHTTPHandler)
        self.server.response_body = response_body
        self.server.paths = []
        self.server.block_first = block_first
        self.server.entered = threading.Event()
        self.server.release = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class SummarizeTest(unittest.TestCase):
    def test_parses_json_string_columns(self) -> None:
        row = pm.summarize_market(SAMPLE)
        self.assertEqual(row["yes"], 0.0445)
        self.assertEqual(row["end"], "2026-12-31")
        self.assertEqual(row["fee_rate"], 0.04)
        self.assertEqual(row["category"], "Crypto")

    def test_formatters(self) -> None:
        self.assertEqual(pm.format_yes(0.0445), "4.5%")
        self.assertEqual(pm.format_vol(22747.236865), "22.7k")
        self.assertEqual(pm.format_vol(1_200_000), "1.20M")
        detail = pm.format_detail(pm.summarize_market(SAMPLE))
        self.assertIn("YES 4.5%", detail)
        self.assertIn("Read-only market data", detail)
        self.assertNotIn("(", detail.split("\n")[0])

    def test_malformed_gamma_columns_do_not_crash(self) -> None:
        row = pm.summarize_market({
            "id": "bad",
            "question": "Malformed market",
            "outcomePrices": "not-json",
            "outcomes": {"unexpected": True},
            "feeSchedule": "not-json",
        })
        self.assertIsNone(row["yes"])
        self.assertIsNone(row["fee_rate"])

    def test_preserves_source_language_in_gamma_fields(self) -> None:
        raw = {
            **SAMPLE,
            "question": "\u6bd4\u7279\u5e01\u4f1a\u4e0a\u6da8\u5417\uff1f",
            "slug": "bitcoin-\u9884\u6d4b",
            "category": "\u52a0\u5bc6\u8d27\u5e01",
        }
        row = pm.summarize_market(raw)
        self.assertEqual(row["question"], raw["question"])
        self.assertEqual(row["slug"], raw["slug"])
        self.assertEqual(row["category"], raw["category"])
        self.assertIn(raw["question"], pm.format_detail(row))


class ProxyEgressTest(unittest.TestCase):
    def test_accepts_only_explicit_http_proxy_urls(self) -> None:
        self.assertEqual(pm.as_http_proxy("http://proxy.example"),
                         "http://proxy.example")
        with self.assertRaisesRegex(ValueError, "http://"):
            pm.as_http_proxy("https://proxy.example")
        with self.assertRaisesRegex(ValueError, "http://"):
            pm.as_http_proxy("socks5://proxy.example")
        with self.assertRaisesRegex(ValueError, "credentials"):
            pm.as_http_proxy("http://user:secret@proxy.example")
        with self.assertRaisesRegex(ValueError, "one proxy origin"):
            pm.as_http_proxy("http://proxy.example/path")
        with self.assertRaisesRegex(ValueError, "empty"):
            pm.as_http_proxy("")

    def test_explicit_argument_wins_over_environment(self) -> None:
        with mock.patch.dict(os.environ, {
            "STAMMTISCH_POLYMARKET_PROXY": "http://env-proxy.example",
        }):
            egress = pm.resolve_egress("http://config-proxy.example")
        self.assertIsNotNone(egress)
        self.assertEqual(egress.url, "http://config-proxy.example")
        self.assertEqual(egress.label, "configured proxy")

    def test_product_specific_environment_is_supported(self) -> None:
        with mock.patch.dict(os.environ, {
            "STAMMTISCH_POLYMARKET_PROXY": "http://env-proxy.example",
        }):
            egress = pm.resolve_egress()
        self.assertIsNotNone(egress)
        self.assertEqual(egress.url, "http://env-proxy.example")

    def test_ambient_proxy_variables_are_ignored(self) -> None:
        with mock.patch.dict(os.environ, {
            "STAMMTISCH_POLYMARKET_PROXY": "",
            "HTTP_PROXY": "http://ambient.example",
            "HTTPS_PROXY": "http://ambient.example",
            "ALL_PROXY": "socks5://ambient.example",
        }):
            self.assertIsNone(pm.resolve_egress())

    def test_invalid_explicit_proxy_is_none(self) -> None:
        self.assertIsNone(pm.resolve_egress("socks5://proxy.example"))
        self.assertIsNone(pm.resolve_egress("https://proxy.example"))

    def test_explicit_proxy_ignores_no_proxy_without_global_concurrent_pollution(self) -> None:
        """The product request is proxied while an unrelated opener stays direct."""
        with (
            _LocalHTTPServer(b"TARGET") as target,
            _LocalHTTPServer(b"PROXY", block_first=True) as proxy,
            mock.patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": proxy.url,
                    "http_proxy": proxy.url,
                    "HTTPS_PROXY": "",
                    "https_proxy": "",
                    "ALL_PROXY": "",
                    "all_proxy": "",
                    "NO_PROXY": "127.0.0.1,localhost",
                    "no_proxy": "127.0.0.1,localhost",
                },
            ),
        ):
            target_url = target.url + "/markets"
            with ThreadPoolExecutor(max_workers=2) as pool:
                pinned = pool.submit(pm._open, target_url, proxy.url, 5.0)
                self.assertTrue(proxy.server.entered.wait(timeout=5))

                # Build a normal ambient opener while the pinned request is
                # in flight.  NO_PROXY must still make this unrelated request
                # direct; a process-global monkeypatch would send it to proxy.
                with build_opener().open(target_url, timeout=5) as response:
                    unrelated = response.read()
                proxy.server.release.set()
                proxied = pinned.result(timeout=5)

        self.assertEqual(proxied, b"PROXY")
        self.assertEqual(unrelated, b"TARGET")
        self.assertEqual(target.server.paths, ["/markets"])
        self.assertEqual(proxy.server.paths, [target_url])


class FetchTest(unittest.TestCase):
    def test_missing_proxy_fails_closed_without_opening_network(self) -> None:
        with mock.patch.dict(os.environ, {"STAMMTISCH_POLYMARKET_PROXY": ""}):
            with mock.patch.object(pm, "_open") as open_spy:
                result = pm.fetch_markets()
        self.assertFalse(result["ok"])
        self.assertIn("direct network access is disabled", result["error"])
        open_spy.assert_not_called()

    def test_invalid_proxy_fails_closed_with_distinct_error(self) -> None:
        with mock.patch.object(pm, "_open") as open_spy:
            result = pm.fetch_markets(proxy_url="socks5://proxy.example")
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "invalid Polymarket proxy configuration; direct network access is disabled",
        )
        open_spy.assert_not_called()

    def test_io_seam_rejects_direct_access(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit proxy URL"):
            pm._open(pm.GAMMA_BASE, "")

    def test_fetch_uses_explicit_proxy_without_exposing_endpoint(self) -> None:
        seen = {}
        payload = json.dumps([SAMPLE]).encode()

        def fake_open(url, proxy=None, timeout=20.0):
            seen["proxy"] = proxy
            self.assertIn("/markets?", url)
            self.assertIn("order=volume24hr", url)
            return payload

        with mock.patch.object(pm, "_open", fake_open):
            result = pm.fetch_markets(proxy_url="http://proxy.example")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(seen["proxy"], "http://proxy.example")
        self.assertEqual(result["via"], "configured proxy")
        self.assertNotIn("proxy.example", result["via"])
        self.assertEqual(result["markets"][0]["question"], SAMPLE["question"])

    def test_fetch_timeout_has_generic_error(self) -> None:
        def boom(url, proxy=None, timeout=20.0):
            raise TimeoutError("timed out")

        with mock.patch.object(pm, "_open", boom):
            result = pm.fetch_markets(proxy_url="http://proxy.example")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "request timed out through the configured proxy")
        self.assertNotIn("proxy.example", result["error"])

    def test_wrapped_timeout_has_generic_error(self) -> None:
        def boom(url, proxy=None, timeout=20.0):
            raise URLError(TimeoutError("timed out"))

        with mock.patch.object(pm, "_open", boom):
            result = pm.fetch_markets(proxy_url="http://proxy.example")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "request timed out through the configured proxy")

    def test_connection_refused_reports_unavailable_proxy(self) -> None:
        def boom(url, proxy=None, timeout=20.0):
            raise URLError(ConnectionRefusedError(111, "localized detail"))

        with mock.patch.object(pm, "_open", boom):
            result = pm.fetch_markets(proxy_url="http://proxy.example")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "configured proxy is unavailable")
        self.assertNotIn("localized detail", result["error"])

    def test_fetch_error_does_not_expose_localized_os_message(self) -> None:
        localized_message = "\u4ee3\u7406\u8fde\u63a5\u5931\u8d25"

        def boom(url, proxy=None, timeout=20.0):
            raise OSError(localized_message)

        with mock.patch.object(pm, "_open", boom):
            result = pm.fetch_markets(proxy_url="http://proxy.example")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "request failed through the configured proxy")
        self.assertNotIn(localized_message, result["error"])


class FilterTest(unittest.TestCase):
    def test_matches_question_and_slug(self) -> None:
        row = pm.summarize_market(SAMPLE)
        self.assertTrue(pm._matches(row, "btc"))
        self.assertTrue(pm._matches(row, "CRYPTO"))
        self.assertFalse(pm._matches(row, "election"))


class MarketUrlTest(unittest.TestCase):
    def test_slug_builds_anonymous_public_page(self) -> None:
        row = pm.summarize_market(SAMPLE)
        url = pm.market_url(row)
        self.assertIsNotNone(url)
        self.assertTrue(url.startswith("https://polymarket.com/market/"))
        self.assertIn(row["slug"], url)

    def test_missing_slug_yields_no_url(self) -> None:
        self.assertIsNone(pm.market_url({"slug": ""}))
        self.assertIsNone(pm.market_url({}))


class FrameworkLanguageTest(unittest.TestCase):
    def test_static_tool_chrome_contains_no_han_characters(self) -> None:
        """Keep framework copy English without policing upstream content."""
        root = Path(__file__).resolve().parents[1]
        framework_files = (
            "tui/app.py",
            "tui/analysis.py",
            "tui/config_cli.py",
            "tui/deepseek.py",
            "tui/polymarket.py",
            "tui/screens.py",
            "tui/theme.py",
            "tui/web_chart.html",
            "tui/widgets.py",
        )
        offenders = []
        for relative in framework_files:
            path = root / relative
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if any("\u3400" <= char <= "\u9fff" for char in line):
                    offenders.append(f"{relative}:{lineno}")
        self.assertEqual(offenders, [], "Han characters found: " + ", ".join(offenders))

    def test_public_framework_has_no_private_infrastructure_markers(self) -> None:
        root = Path(__file__).resolve().parents[1]
        framework_files = (
            "tui/config.py",
            "tui/polymarket.py",
            "tui/screens.py",
            "tui/README.md",
            "README.md",
        )
        forbidden = tuple(
            "".join(parts)
            for parts in (
                ("CAUSE", "WAY"),
                ("Cloud", "Storage"),
                ("Documents/", "Private"),
                ("Documents/", "Public"),
            )
        )
        offenders = []
        for relative in framework_files:
            text = (root / relative).read_text(encoding="utf-8")
            for marker in forbidden:
                if marker.casefold() in text.casefold():
                    offenders.append(f"{relative}: {marker}")
        self.assertEqual(offenders, [], "Private markers found: " + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main()
