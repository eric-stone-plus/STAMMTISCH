"""Terminal experience — text charts, palette, feeds panel, watchlist."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import pandas as pd
from textual.app import App
from textual.widgets import DataTable, Static

from tui.charts import TerminalChartScreen, df_to_candles, render_candles
from tui.command_palette import WorkstationCommands
from services.datafeeds import registry
from tui.screens.domains import _watchlist_add, _watchlist_remove
from tui.screens.feeds import FeedHealthScreen


def _candles(count: int = 30) -> list[dict]:
    out = []
    for index in range(count):
        base = 100 + index
        out.append({
            "time": f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}",
            "open": float(base),
            "high": float(base + 5),
            "low": float(base - 4),
            "close": float(base + 2),
            "volume": float(1000 * (index + 1)),
        })
    return out


class CandleRendererTest(unittest.TestCase):
    def test_rows_fill_dimensions(self):
        rows = render_candles(_candles(40), 70, 18)
        self.assertEqual(len(rows), 18)
        self.assertTrue(all(len(row.plain) <= 70 for row in rows))

    def test_candles_render_wicks_and_volume(self):
        candles = [
            {"time": "2026-01-01", "open": 10, "high": 12, "low": 9,
             "close": 11, "volume": 100},
            {"time": "2026-01-02", "open": 11, "high": 13, "low": 10,
             "close": 10.5, "volume": 200},
        ]
        rows = render_candles(candles, 40, 12)
        plain = "\n".join(row.plain for row in rows)
        self.assertIn("│", plain)  # wicks
        self.assertIn("2026-01-01", plain)  # header carries the date range

    def test_empty_and_degenerate_input(self):
        self.assertEqual(len(render_candles([], 60, 10)), 10)
        flat = [{"time": "d", "open": 5, "high": 5, "low": 5, "close": 5,
                 "volume": 1}]
        rows = render_candles(flat, 60, 10)
        self.assertEqual(len(rows), 10)
        self.assertIn("no price range", rows[0].plain)

    def test_df_to_candles(self):
        df = pd.DataFrame({
            "open": [10.0], "high": [12.0], "low": [9.0],
            "close": [11.0], "volume": [123.0],
        }, index=pd.DatetimeIndex(["2026-01-02"], name="date"))
        candles = df_to_candles(df)
        self.assertEqual(candles[0]["close"], 11.0)
        self.assertEqual(candles[0]["time"], "2026-01-02")

    def test_df_to_candles_rejects_non_ohlc(self):
        df = pd.DataFrame({"price": [1.0]})
        self.assertIsNone(df_to_candles(df))
        self.assertIsNone(df_to_candles(None))


class _HostAppHarness(unittest.TestCase):
    """Screens need a running App; host them on a minimal shell."""

    def _host(self, config_file: str | None = None):
        from textual.app import App, ComposeResult
        from textual.widgets import Static as _Static

        class _Host(App):
            def compose(self) -> ComposeResult:
                yield _Static("")

        return _Host()


class TerminalChartScreenTest(_HostAppHarness):
    def test_injected_candles_render_with_source_label(self):
        asyncio.run(self._scenario())

    async def _scenario(self) -> None:
        host = self._host()
        async with host.run_test() as pilot:
            host.push_screen(TerminalChartScreen(
                engine=None, config=None, symbol="AAPL", candles=_candles()))
            await pilot.pause()
            screen = host.app.screen if hasattr(host, "app") else host.screen
            head = screen.query_one("#tc-head", Static)
            self.assertIn("AAPL", str(head.render()))
            self.assertIn("caller-provided bars", str(head.render()))
            from tui.charts import CandleChart

            chart = screen.query_one("#tc-chart", CandleChart)
            self.assertEqual(len(chart.candles), 30)


class FeedHealthScreenTest(_HostAppHarness):
    def test_provider_rows_and_cache_line(self):
        asyncio.run(self._scenario())

    async def _scenario(self) -> None:
        registry.reset_stats()
        registry.tracked("tencent", lambda: "ok")

        def boom():
            raise RuntimeError("down")
        with self.assertRaises(RuntimeError):
            registry.tracked("yahoo", boom)
        host = self._host()
        async with host.run_test() as pilot:
            host.push_screen(FeedHealthScreen(config=None))
            await pilot.pause()
            screen = host.screen
            table = screen.query_one("#feed-table", DataTable)
            rows = {
                str(table.get_row_at(row)[0]): table.get_row_at(row)
                for row in range(table.row_count)
            }
            self.assertIn("tencent", rows)
            self.assertEqual(rows["tencent"][1], "1")
            self.assertIn("yahoo", rows)
            self.assertEqual(rows["yahoo"][2], "1")
            self.assertIn("down", rows["yahoo"][5])
            footer = screen.query_one("#feed-cache", Static)
            self.assertIn("cache:", str(footer.render()))


class PaletteProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._config_path = os.path.join(self._tmp.name, "config.json")
        with open(self._config_path, "w") as handle:
            json.dump({"state_root": os.path.join(self._tmp.name, "state")}, handle)
        os.environ["STAMMTISCH_CONFIG"] = self._config_path
        for key in ("GLM_API_KEY", "ZHIPU_API_KEY", "XIAOMI_API_KEY",
                    "DEEPSEEK_API_KEY", "DEEPSEEK_KEY", "DEEPSEEK_TOKEN",
                    "QIANWEN_TP_PERSONAL_KEY", "EIA_API_KEY"):
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        os.environ.pop("STAMMTISCH_CONFIG", None)
        self._tmp.cleanup()

    def test_discover_lists_workbench_commands(self):
        asyncio.run(self._scenario())

    async def _scenario(self) -> None:
        from tui.app import StammtischTUI

        app = StammtischTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            provider = WorkstationCommands(app.screen)
            commands = [hit.command async for hit in provider.discover()]
            self.assertGreaterEqual(len(commands), 9)
            labels = [str(hit.display) async for hit in provider.discover()]
            self.assertTrue(any("Backtest" in label for label in labels), labels)

    def test_symbol_search_resolves_offline(self):
        asyncio.run(self._symbol_scenario())

    async def _symbol_scenario(self) -> None:
        from tui.app import StammtischTUI

        app = StammtischTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            provider = WorkstationCommands(app.screen)
            hits = [hit async for hit in provider.search("apple")]
            labels = [str(hit.match_display) for hit in hits]
            self.assertTrue(any("AAPL" in label for label in labels), labels)
            # Exact-ticker typing always offers the jump.
            hits = [hit async for hit in provider.search("nvda")]
            labels = [str(hit.match_display) for hit in hits]
            self.assertTrue(any("NVDA" in label for label in labels), labels)


class WatchlistRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._config_path = os.path.join(self._tmp.name, "config.json")
        with open(self._config_path, "w") as handle:
            json.dump({"security_symbols": ["MSFT"]}, handle)
        os.environ["STAMMTISCH_CONFIG"] = self._config_path

    def tearDown(self) -> None:
        os.environ.pop("STAMMTISCH_CONFIG", None)
        self._tmp.cleanup()

    def _config(self):
        from services.config import Config

        return Config()

    def test_add_normalizes_dedupes_and_persists(self):
        config = self._config()
        symbol, message, duplicate = _watchlist_add(config, " aapl ")
        self.assertEqual(symbol, "AAPL")
        self.assertFalse(duplicate)
        self.assertEqual(config.get("security_symbols"), ["MSFT", "AAPL"])
        _symbol, _message, duplicate = _watchlist_add(config, "AAPL")
        self.assertTrue(duplicate)
        self.assertEqual(config.get("security_symbols"), ["MSFT", "AAPL"])
        # Bare A-share codes normalize onto the anchored list too.
        symbol, _message, _duplicate = _watchlist_add(config, "600519")
        self.assertEqual(symbol, "600519.SS")
        self.assertIn("600519.SS", config.get("security_symbols"))

    def test_remove_only_unanchors_manual_symbols(self):
        config = self._config()
        removed, _message = _watchlist_remove(config, "MSFT")
        self.assertTrue(removed)
        self.assertEqual(config.get("security_symbols"), [])
        removed, message = _watchlist_remove(config, "MSFT")
        self.assertFalse(removed)
        self.assertIn("not an anchored symbol", message)


if __name__ == "__main__":
    unittest.main()


class CryptoBoardScreenTest(unittest.TestCase):
    def test_spark_chars_shape(self):
        from tui.screens.crypto_board import spark_chars

        self.assertEqual(spark_chars([]), "")
        self.assertEqual(spark_chars([5, 5]), "▄▄")
        out = spark_chars([1, 2, 3, 4, 5])
        self.assertEqual(len(out), 5)
        self.assertEqual(out[0], "▁")
        self.assertEqual(out[-1], "█")

    def test_board_renders_with_mocked_chain(self):
        asyncio.run(self._scenario())

    async def _scenario(self) -> None:
        from textual.widgets import DataTable, Static

        from tui.screens.crypto_board import CryptoBoardScreen

        board = {"rows": [
            {"symbol": "BTC", "name": "Bitcoin", "last": 60000.0,
             "chg_24h": 1.5, "market_cap": 1.2e12, "volume": 3e10,
             "spark": [1, 2, 3, 4, 5], "source": "coingecko"},
            {"symbol": "ETH", "name": "Ethereum", "last": 3000.0,
             "chg_24h": -2.0, "market_cap": 4e11, "volume": 1.5e10,
             "spark": [], "source": "coingecko"},
        ], "btc_dominance": 55.0}
        with mock.patch("services.datafeeds.service.crypto_board",
                        return_value=board):
            host_screen = CryptoBoardScreen(engine=None, config=None)

            class Host(App):
                pass

            async with Host().run_test() as pilot:
                host = pilot.app
                host.push_screen(host_screen)
                await pilot.pause()
                await pilot.pause()
                table = host_screen.query_one("#coins-table", DataTable)
                self.assertEqual(table.row_count, 2)
                status = host_screen.query_one("#coins-status", Static)
                self.assertIn("BTC dominance 55.0%", str(status.render()))
                self.assertIn("coingecko", str(status.render()))

    def test_price_rendering_is_never_scientific(self):
        from tui.screens.crypto_board import _price

        self.assertEqual(_price(84590.0), "84,590.00")
        self.assertEqual(_price(1234567.891), "1,234,567.89")
        self.assertEqual(_price(3000.0), "3,000.00")
        self.assertEqual(_price(1.23456), "1.2346")
        self.assertEqual(_price(0.9999), "0.99990000")
        self.assertEqual(_price(0.00001234), "0.00001234")
        self.assertEqual(_price(0.0), "—")
        for value in (1e-8, 5e-4, 1.0, 999.99, 8.459e4, 1e9, 1e12, 9.9e13):
            rendered = _price(value)
            self.assertNotIn("e", rendered.lower(), rendered)

    def test_age_text_and_stamp_parsing(self):
        from tui.screens.crypto_board import _age_text, _parse_epoch

        self.assertEqual(_age_text(5), "5s")
        self.assertEqual(_age_text(65), "1m05s")
        self.assertEqual(_age_text(3725), "1h02m")
        self.assertEqual(_age_text(-3), "0s")
        self.assertIsNone(_parse_epoch("2026-09-25T10:00:00"))  # naive: refused
        self.assertIsNone(_parse_epoch("garbage"))
        self.assertIsNone(_parse_epoch(None))
        self.assertIsNotNone(_parse_epoch("2026-09-25T10:00:00+00:00"))

    def test_status_carries_data_age_and_yields_to_operational(self):
        asyncio.run(self._age_scenario())

    async def _age_scenario(self) -> None:
        from datetime import datetime, timedelta, timezone

        from textual.widgets import Static

        from tui.screens.crypto_board import CryptoBoardScreen

        generated = (datetime.now(timezone.utc)
                     - timedelta(seconds=5)).isoformat(timespec="seconds")
        board = {"rows": [
            {"symbol": "BTC", "name": "Bitcoin", "last": 84590.0,
             "chg_24h": 1.5, "market_cap": 1.2e12, "volume": 3e10,
             "spark": [], "source": "coingecko"},
        ], "btc_dominance": 55.0, "generated_at": generated}
        with mock.patch("services.datafeeds.service.crypto_board",
                        return_value=board):
            host_screen = CryptoBoardScreen(engine=None, config=None)

            class Host(App):
                pass

            async with Host().run_test() as pilot:
                host = pilot.app
                host.push_screen(host_screen)
                await pilot.pause()
                await pilot.pause()
                status = host_screen.query_one("#coins-status", Static)
                rendered = str(status.render())
                self.assertIn("data ", rendered)
                self.assertIn("ago)", rendered)
                # The price cell stays fixed-point end to end.
                table = host_screen.query_one("#coins-table", DataTable)
                self.assertEqual(str(table.get_cell_at((0, 2))), "84,590.00")
                # Operational messages own the line; the ticker must not
                # clobber them on the next render.
                host_screen._set_status("  screener running")
                host_screen._render_board_status()
                self.assertIn("screener running",
                              str(status.render()))

    def test_unstamped_cache_payload_never_fakes_data_age(self):
        asyncio.run(self._unstamped_scenario())

    async def _unstamped_scenario(self) -> None:
        from textual.widgets import Static

        from tui.screens.crypto_board import CryptoBoardScreen

        # Disk-cache snapshots predating the generated_at stamp carry no
        # data age; the render moment must not be passed off as the
        # data's.
        board = {"rows": [
            {"symbol": "BTC", "name": "Bitcoin", "last": 84590.0,
             "chg_24h": 0.0, "market_cap": 1.2e12, "volume": 3e10,
             "spark": [], "source": "coingecko"},
        ], "btc_dominance": None}
        with mock.patch("services.datafeeds.service.crypto_board",
                        return_value=board):
            host_screen = CryptoBoardScreen(engine=None, config=None)

            class Host(App):
                pass

            async with Host().run_test() as pilot:
                host = pilot.app
                host.push_screen(host_screen)
                await pilot.pause()
                await pilot.pause()
                rendered = str(host_screen.query_one("#coins-status",
                                                     Static).render())
        self.assertIn("age unknown", rendered)
        self.assertNotIn("ago)", rendered)

    def test_k_binding_opens_browser_chart_on_crypto_route(self):
        asyncio.run(self._k_scenario())

    async def _k_scenario(self) -> None:
        from tui.screens.crypto_board import CryptoBoardScreen

        board = {"rows": [
            {"symbol": "BTC", "name": "Bitcoin", "last": 84590.0,
             "chg_24h": 0.0, "market_cap": 1.2e12, "volume": 3e10,
             "spark": [], "source": "coingecko"},
        ], "btc_dominance": None}
        with mock.patch("services.datafeeds.service.crypto_board",
                        return_value=board), \
             mock.patch("tui.screens.domains._open_browser_chart") as opener:
            host_screen = CryptoBoardScreen(engine=None, config=None)

            class Host(App):
                pass

            async with Host().run_test() as pilot:
                host = pilot.app
                host.push_screen(host_screen)
                await pilot.pause()
                await pilot.pause()
                await pilot.press("k")
                await pilot.pause()
        opener.assert_called_once()
        self.assertEqual(opener.call_args[0][2], "CRYPTO:BTC")
