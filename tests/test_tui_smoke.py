"""TUI smoke tests — headless Textual pilot runs (no terminal needed).

Verifies the interaction layer: clickable sidebar @click tags, the
[E]dit config screen, and hot-apply of a saved API key.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from textual.widgets import DataTable, Input, OptionList, Static

from pathlib import Path

from tui.app import StammtischTUI
from tui.screens import ConfigScreen, DashboardScreen


class TuiSmokeTest(unittest.TestCase):
    def test_clickable_menus_and_edit_screen(self) -> None:
        asyncio.run(self._scenario())

    def test_confirm_screen_keyboard_and_mouse(self) -> None:
        asyncio.run(self._confirm_scenario())

    def test_pipeline_viewer_screen(self) -> None:
        asyncio.run(self._pipeline_view_scenario())

    def test_brief_screen(self) -> None:
        asyncio.run(self._brief_scenario())

    def test_daily_intake_screen(self) -> None:
        asyncio.run(self._daily_intake_scenario())

    def test_daily_intake_rejected_screen(self) -> None:
        asyncio.run(self._daily_intake_rejected_scenario())

    def test_report_history_screen(self) -> None:
        asyncio.run(self._report_history_scenario())

    def test_sentiment_key_is_market_wide(self) -> None:
        asyncio.run(self._sentiment_scenario())

    def test_polymarket_failure_is_actionable(self) -> None:
        asyncio.run(self._polymarket_failure_scenario())

    def test_crypto_screen_title_and_fetch(self) -> None:
        asyncio.run(self._crypto_scenario())

    def test_crypto_daily_desk_switch(self) -> None:
        asyncio.run(self._crypto_daily_scenario())

    def test_energy_screen_key_and_fetch(self) -> None:
        asyncio.run(self._energy_scenario())

    def test_casino_hosts_racing_board(self) -> None:
        asyncio.run(self._casino_scenario())

    def test_security_zone_classifier(self) -> None:
        from tui.screens import security_zone

        self.assertEqual(security_zone("600188.SS"), "A-SHARE")
        self.assertEqual(security_zone("601088.ss"), "A-SHARE")
        self.assertEqual(security_zone("000001.SZ"), "A-SHARE")
        self.assertEqual(security_zone("430047.BJ"), "A-SHARE")
        self.assertEqual(security_zone("1088.HK"), "HK")
        self.assertEqual(security_zone("BTU"), "US")
        self.assertEqual(security_zone("AAPL"), "US")
        self.assertEqual(security_zone("7203.T"), "OTHER")

    def test_security_board_zones(self) -> None:
        asyncio.run(self._security_scenario())

    def test_domain_plugins_menu_and_browser(self) -> None:
        asyncio.run(self._domain_plugins_scenario())

    def test_domain_browser_missing_root(self) -> None:
        asyncio.run(self._domain_browser_missing_scenario())

    def test_domain_browser_listing_handles_bad_roots(self) -> None:
        from tui.screens import DomainBrowserScreen

        with tempfile.TemporaryDirectory() as tmp:
            missing = DomainBrowserScreen(
                "GHOST", os.path.join(tmp, "nope")
            )._listing()
            self.assertIn("does not exist", missing)

            file_path = os.path.join(tmp, "file.txt")
            Path(file_path).write_text("x", encoding="utf-8")
            not_dir = DomainBrowserScreen("FILE", file_path)._listing()
            self.assertIn("not a directory", not_dir)

            empty_dir = os.path.join(tmp, "empty")
            os.mkdir(empty_dir)
            self.assertIn("(empty)", DomainBrowserScreen("EMPTY", empty_dir)._listing())

    def test_plugins_list_click_maps_to_the_clicked_row(self) -> None:
        asyncio.run(self._plugins_click_scenario())

    def test_quick_list_click_maps_to_the_clicked_row(self) -> None:
        asyncio.run(self._quick_click_scenario())

    def test_list_keyboard_highlight_survives_pointer_leave(self) -> None:
        asyncio.run(self._leave_clears_scenario())

    def test_pipeline_run_screen_requires_explicit_run(self) -> None:
        asyncio.run(self._pipeline_run_scenario())

    def test_pipeline_run_result_is_bound_to_starting_selection(self) -> None:
        asyncio.run(self._pipeline_run_selection_isolation_scenario())

    def test_dashboard_status_failure_has_no_manifest_fallback(self) -> None:
        asyncio.run(self._dashboard_status_failure_scenario())

    def test_run_inspector_uses_one_verified_snapshot(self) -> None:
        asyncio.run(self._run_inspector_snapshot_scenario())

    def test_run_inspector_fails_closed_on_inspect_error(self) -> None:
        asyncio.run(self._run_inspector_error_scenario())

    def test_daily_intake_invalid_config_fails_visibly(self) -> None:
        asyncio.run(self._intake_invalid_config_scenario())

    def test_intake_session_progress_stays_on_home(self) -> None:
        asyncio.run(self._intake_session_progress_scenario())

    def test_intake_workspace_observation(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from tui.intake_job import (
            observe_workspace,
            render_progress,
            report_session_date,
            session_title,
        )

        self.assertEqual(
            session_title("20260818", "2026-08-18T16:58:00Z"),
            "daily-intake · 2026-08-18 · 16:58",
        )
        shanghai = ZoneInfo("Asia/Shanghai")
        pre_open = datetime(2026, 8, 19, 0, 50, tzinfo=shanghai)
        after_open = datetime(2026, 8, 19, 10, 0, tzinfo=shanghai)
        # The offline exchange calendar is the only date authority: with it
        # the pre-open capture belongs to the previous session; without it
        # there is no answer at all — a weekday or fixed-clock guess is
        # what the intake contract forbids, so the product certifies the
        # date instead (no `--date` is passed).
        try:
            import exchange_calendars  # noqa: F401
            import pandas  # noqa: F401
        except ImportError:
            have_calendar = False
        else:
            have_calendar = True
        if have_calendar:
            self.assertEqual(report_session_date(pre_open), "20260818")
            self.assertEqual(report_session_date(after_open), "20260819")
        else:
            self.assertIsNone(report_session_date(pre_open))
            self.assertIsNone(report_session_date(after_open))
            with mock.patch(
                "tui.intake_job._calendar_session_date", return_value="20260817"
            ) as calendar:
                self.assertEqual(report_session_date(pre_open), "20260817")
                calendar.assert_called_once()
            # An unresolved session date still renders a display label...
            self.assertTrue(session_title("", "2026-08-19T09:05:00Z").startswith(
                "daily-intake · "
            ))
            # ...and an empty date means "the product certifies it", never
            # a guess.
            from tui.intake_job import empty_session

            with tempfile.TemporaryDirectory() as tmp:
                session = empty_session(tmp)
                self.assertEqual(session["date"], "")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markdown = root / "runs" / "rid" / "normalized-markdown"
            markdown.mkdir(parents=True)
            (markdown / "eastmoney.md").write_text("x", encoding="utf-8")
            seen = observe_workspace(root, time.time() - 10, "20260818")
            self.assertGreaterEqual(seen["accepted"], 1)
            self.assertIn("eastmoney", seen["summary"])
            text = render_progress({
                "state": "capturing",
                "elapsed_s": 75,
                "title": "daily-intake · 2026-08-18",
                "summary": seen["summary"],
                "lines": seen["lines"],
            })
            self.assertIn("CAPTURING", text)
            self.assertIn("1m 15s", text)
            self.assertIn("daily-intake · 2026-08-18", text)

    def test_chat_input_page_keys_scroll_the_log(self) -> None:
        asyncio.run(self._chat_page_scenario())

    def test_run_session_title_uses_pipeline_and_time(self) -> None:
        from tui.screens import run_session_title

        self.assertEqual(
            run_session_title({
                "pipeline_id": "stock-quinte-local",
                "created_at": "2026-08-18T03:25:06.320Z",
                "run_id": "01a012e6-bb8c-75f3-8597-c55291876dc5",
            }),
            "stock-quinte-local  2026-08-18 03:25",
        )
        self.assertEqual(run_session_title({"title": " coal screen "}), "coal screen")

    def test_dashboard_delete_selected_run(self) -> None:
        asyncio.run(self._dashboard_delete_scenario())

    def test_dashboard_intake_rows_and_locked_delete(self) -> None:
        asyncio.run(self._intake_rows_delete_scenario())

    def test_dashboard_shift_click_selects_range(self) -> None:
        asyncio.run(self._dashboard_shift_select_scenario())

    def test_security_board_reuses_cached_quotes(self) -> None:
        asyncio.run(self._security_cache_scenario())

    async def _polymarket_failure_scenario(self) -> None:
        from tui.polymarket import PolymarketScreen

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({
                    "polymarket_proxy_url": "http://proxy.example",
                    "state_root": os.path.join(tmp, "state"),
                }, handle)
            failed = {
                "ok": False,
                "markets": [],
                "error": "configured proxy is unavailable",
                "via": "configured proxy",
            }
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch("tui.polymarket.fetch_markets", return_value=failed):
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.pause()
                        app.push_screen(PolymarketScreen(proxy_url="http://proxy.example"))
                        await pilot.pause()
                        body = str(app.screen.query_one("#pm-status", Static).render())
                        self.assertIn("configured proxy is unavailable", body)
                        self.assertIn("Market Proxy in Settings", body)
                        self.assertNotIn("proxy.example", body)

    async def _crypto_scenario(self) -> None:
        from tui.polymarket import CryptoScreen

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({"state_root": os.path.join(tmp, "state")}, handle)
            fake_pm = {
                "ok": True,
                "markets": [{
                    "id": "1",
                    "question": "Will ETH flip BTC?",
                    "slug": "eth-flip-btc",
                    "yes": 0.11,
                    "volume24hr": 9900.0,
                    "volume": 5e5,
                    "end": "2026-12-31",
                    "fee_rate": 0.0,
                    "category": "Crypto",
                }],
                "error": None,
            }
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch(
                    "tui.polymarket.fetch_markets", return_value=fake_pm
                ) as fetch_spy:
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.pause()
                        app.push_screen(CryptoScreen(proxy_url="http://proxy.example"))
                        await pilot.pause()
                        for _ in range(20):
                            await asyncio.sleep(0.05)
                            await pilot.pause()
                            if "active markets" in str(
                                app.screen.query_one("#pm-status", Static).render()
                            ):
                                break
                        # The crypto module reuses the Polymarket fetch and
                        # repaints the header with its own title.
                        self.assertGreaterEqual(fetch_spy.call_count, 1)
                        header = str(
                            app.screen.query_one(".header-bar", Static).render()
                        )
                        self.assertIn("CRYPTO", header)
                        status = str(
                            app.screen.query_one("#pm-status", Static).render()
                        )
                        self.assertIn("1 active markets", status)
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DashboardScreen)

    async def _crypto_daily_scenario(self) -> None:
        from tui.polymarket import CryptoScreen

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            run_dir = workspace / "runs" / "20260814T115124Z-234ed0a7"
            run_dir.mkdir(parents=True)
            (run_dir / "fin-daily-20260814.json").write_text(json.dumps({
                "schema": "stammtisch.daily-report.v1",
                "date": "20260814",
                "run_id": "20260814T115124Z-234ed0a7",
                "brief": [],
                "markets": {
                    "ashare": [], "hk": [], "us": [],
                    "crypto": [{
                        "title": "Bitcoin 突破关键阻力位",
                        "url": "https://example.test/btc",
                        "summary": "ETF 资金流入推动。",
                        "sources": ["CoinDesk"],
                    }],
                },
                "market_counts": {"crypto": 1},
                "notes": [],
            }, ensure_ascii=False), encoding="utf-8")
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({
                    "state_root": os.path.join(tmp, "state"),
                    "data_dir": os.path.join(tmp, "data"),
                    "workspace_root": str(workspace),
                    "reports_root": os.path.join(tmp, "legacy"),
                }, handle)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch(
                    "tui.polymarket.fetch_markets",
                    return_value={"ok": True, "markets": [], "error": None},
                ):
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.pause()
                        app.push_screen(
                            CryptoScreen(proxy_url=None, config=app.screen.config)
                        )
                        await pilot.pause()
                        self.assertIn(
                            "POLYMARKET",
                            str(app.screen.query_one(".header-bar", Static).render()),
                        )
                        await pilot.press("right")
                        await pilot.pause()
                        daily = app.screen.query_one("#crypto-daily-wrap")
                        self.assertEqual(daily.styles.display, "block")
                        header = str(
                            app.screen.query_one(".header-bar", Static).render()
                        )
                        self.assertIn("DAILY REPORT", header)
                        body = str(
                            app.screen.query_one("#crypto-daily-text", Static).render()
                        )
                        self.assertIn("2026-08-14", body)
                        self.assertIn("Bitcoin 突破关键阻力位", body)
                        await pilot.press("left")
                        await pilot.pause()
                        self.assertEqual(
                            app.screen.query_one("#crypto-daily-wrap").styles.display,
                            "none",
                        )
                        self.assertEqual(
                            app.screen.query_one("#pm-table-wrap").styles.display,
                            "block",
                        )
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DashboardScreen)

    async def _energy_scenario(self) -> None:
        from tui.energy import EnergyScreen

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({
                    "state_root": os.path.join(tmp, "state"),
                    "eia_api_key": "KEY123",
                    "energy_proxy_url": "http://proxy.example:8080",
                }, handle)
            fake_board = {
                "ok": True,
                "rows": [{
                    "key": "wti_spot",
                    "group": "CRUDE",
                    "label": "WTI Cushing spot",
                    "unit": "$/bbl",
                    "decimals": 2,
                    "frequency": "daily",
                    "period": "2026-08-14",
                    "value": 63.96,
                    "prev_period": "2026-08-13",
                    "prev_value": 64.40,
                    "change": -0.44,
                    "change_pct": -0.68,
                    "history": [("2026-08-14", 63.96), ("2026-08-13", 64.40)],
                    "description": "Cushing, OK WTI Spot Price FOB",
                    "route": "petroleum/pri/spt",
                    "error": None,
                }],
                "error": None,
                "via": "configured proxy",
            }
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch(
                    "tui.energy.fetch_watchlist", return_value=fake_board
                ) as fetch_spy:
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DashboardScreen)
                        # ENERGY is a Plugins-list module, alphabetically
                        # after CRYPTO.
                        pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                        option_labels = [str(option.prompt) for option in pipeline_list.options]
                        self.assertIn("CRYPTO", option_labels)
                        self.assertIn("ENERGY", option_labels)
                        self.assertLess(
                            option_labels.index("CRYPTO"), option_labels.index("ENERGY")
                        )
                        pipeline_list.focus()
                        pipeline_list.highlighted = option_labels.index("ENERGY")
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, EnergyScreen)
                        for _ in range(20):
                            await asyncio.sleep(0.05)
                            await pilot.pause()
                            if "series" in str(
                                app.screen.query_one("#eg-status", Static).render()
                            ):
                                break
                        self.assertGreaterEqual(fetch_spy.call_count, 1)
                        header = str(
                            app.screen.query_one(".header-bar", Static).render()
                        )
                        self.assertIn("ENERGY", header)
                        status = str(
                            app.screen.query_one("#eg-status", Static).render()
                        )
                        self.assertIn("1/1 series", status)
                        table = app.screen.query_one("#eg-table", DataTable)
                        self.assertEqual(table.row_count, 1)
                        # Row highlight fills the detail pane with history.
                        table.focus()
                        await pilot.pause()
                        detail = str(
                            app.screen.query_one("#eg-detail", Static).render()
                        )
                        self.assertIn("WTI Cushing spot", detail)
                        self.assertIn("2026-08-14 63.96", detail)
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DashboardScreen)

    async def _security_scenario(self) -> None:
        from tui.screens import SecurityScreen

        def _quote(last, prev):
            return {
                "last": last,
                "chg_pct": round((last / prev - 1) * 100, 2),
                "pct5": 1.0, "pct20": 2.0, "volume": 1000.0,
                "recent": [{
                    "date": "2026-08-14", "open": prev, "high": last,
                    "low": prev, "close": last, "volume": 1000.0,
                }],
            }

        canned = {
            "ok": True,
            "quotes": {
                "600188.SS": _quote(15.2, 15.0),
                "1088.HK": _quote(38.4, 38.0),
                "BTU": _quote(24.1, 24.0),
                "BADSUSP.SS": {"error": "no data"},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({
                    "state_root": os.path.join(tmp, "state"),
                    "security_symbols": ["600188.SS", "1088.HK", "BTU", "BADSUSP.SS"],
                }, handle)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch.object(SecurityScreen, "_load", lambda self: canned):
                    async with app.run_test(size=(120, 40)) as pilot:
                        await pilot.pause()
                        pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                        labels = [str(option.prompt) for option in pipeline_list.options]
                        pipeline_list.focus()
                        pipeline_list.highlighted = labels.index("SECURITY")
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, SecurityScreen)
                        board = app.screen.query_one("#sec-board", DataTable)
                        for _ in range(40):
                            await asyncio.sleep(0.05)
                            await pilot.pause()
                            if board.row_count:
                                break
                        # Zones in display order: A-SHARE, HK, US (OTHER empty).
                        self.assertEqual(app.screen._zones, ["A-SHARE", "HK", "US"])
                        strip = str(app.screen.query_one("#sec-cats", Static).render())
                        self.assertIn("A-SHARE", strip)
                        # First zone shows the two A-share rows, errors included.
                        self.assertEqual(board.row_count, 2)
                        row = [str(cell) for cell in board.get_row_at(0)]
                        self.assertEqual(row[0], "600188.SS")
                        self.assertEqual(row[1], "15.20")
                        # Recent detail renders for the highlighted row.
                        recent = app.screen.query_one("#sec-recent", DataTable)
                        self.assertGreater(recent.row_count, 0)
                        # ←/→ switches zones.
                        await pilot.press("right")
                        await pilot.pause()
                        row = [str(cell) for cell in board.get_row_at(0)]
                        self.assertEqual(row[0], "1088.HK")
                        await pilot.press("right")
                        await pilot.pause()
                        row = [str(cell) for cell in board.get_row_at(0)]
                        self.assertEqual(row[0], "BTU")
                        # Wrap-around back to A-SHARE.
                        await pilot.press("right")
                        await pilot.pause()
                        row = [str(cell) for cell in board.get_row_at(0)]
                        self.assertEqual(row[0], "600188.SS")
                        await pilot.press("left")
                        await pilot.pause()
                        row = [str(cell) for cell in board.get_row_at(0)]
                        self.assertEqual(row[0], "BTU")
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DashboardScreen)

    async def _casino_scenario(self) -> None:
        from tui.racing import CasinoScreen

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            casino = root / "casino"
            casino.mkdir()
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                "racing_cmd": "fixture-racing",
                "plugins": [{"label": "CASINO", "root": str(casino)}],
            }), encoding="utf-8")
            fake_board = {
                "ok": True,
                "schema": "wagerkit.hkjc-board.v1",
                "meetings": [{"date": "2026-08-16", "course": "ST", "races": 10}],
                "race": {
                    "date": "2026-08-16", "course": "ST", "race_no": 1,
                    "runners": [],
                },
                "model": {"status": "off", "races_available": 0},
            }
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch(
                    "tui.racing.RacingDriver.board", return_value=fake_board
                ):
                    async with app.run_test(size=(120, 40)) as pilot:
                        await pilot.pause()
                        pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                        labels = [str(option.prompt) for option in pipeline_list.options]
                        # RACING merged into CASINO: the CASINO row opens the
                        # race board, retitled.
                        pipeline_list.focus()
                        pipeline_list.highlighted = labels.index("CASINO")
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, CasinoScreen)
                        header = str(
                            app.screen.query_one(".header-bar", Static).render()
                        )
                        self.assertIn("CASINO", header)
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DashboardScreen)

    async def _domain_plugins_scenario(self) -> None:
        from tui.screens import DomainBrowserScreen

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            futures = root / "futures"
            (futures / "strategies").mkdir(parents=True)
            (futures / "notes.md").write_text("domain notes", encoding="utf-8")
            casino = root / "casino"
            casino.mkdir()
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                # Empty futures list pins the legacy directory-browser
                # fallback; a configured list opens the FuturesScreen instead.
                "futures_symbols": [],
                "plugins": [
                    {"label": "futures", "root": str(futures)},
                    {"label": "CASINO", "root": str(casino)},
                ],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)
                    pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                    option_labels = [str(option.prompt) for option in pipeline_list.options]
                    # Configured plugins appear in the middle Plugins list,
                    # labels uppercased by Config.plugins.
                    self.assertIn("FUTURES", option_labels)
                    self.assertIn("CASINO", option_labels)

                    # Selecting FUTURES opens its domain browser.
                    pipeline_list.focus()
                    pipeline_list.highlighted = option_labels.index("FUTURES")
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DomainBrowserScreen)
                    self.assertEqual(app.screen.label, "FUTURES")
                    header = str(app.screen.query_one(".header-bar", Static).render())
                    self.assertIn("FUTURES", header)
                    self.assertIn(str(futures), header)
                    body = str(app.screen.query_one("#domain-text", Static).render())
                    self.assertIn("strategies/", body)
                    self.assertIn("notes.md", body)
                    # Directories sort before plain files.
                    self.assertLess(body.index("strategies/"), body.index("notes.md"))
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

    async def _domain_browser_missing_scenario(self) -> None:
        from tui.screens import DomainBrowserScreen

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({"state_root": os.path.join(tmp, "state")}, handle)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    app.push_screen(
                        DomainBrowserScreen("GHOST", os.path.join(tmp, "no-such-domain"))
                    )
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DomainBrowserScreen)
                    body = str(app.screen.query_one("#domain-text", Static).render())
                    self.assertIn("does not exist", body)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

    def test_futures_screen(self) -> None:
        asyncio.run(self._futures_screen_scenario())

    def test_futures_category_switch(self) -> None:
        asyncio.run(self._futures_category_scenario())

    def test_futures_chart_key_on_exchange_row(self) -> None:
        asyncio.run(self._futures_chart_key_scenario())

    def test_shipping_chart_key(self) -> None:
        asyncio.run(self._shipping_chart_key_scenario())

    def test_shipping_screen(self) -> None:
        asyncio.run(self._shipping_screen_scenario())

    def test_shipping_spval_category(self) -> None:
        asyncio.run(self._shipping_spval_scenario())

    def test_shipping_fallback_to_browser(self) -> None:
        asyncio.run(self._shipping_fallback_scenario())

    async def _futures_screen_scenario(self) -> None:
        from tui.screens import FuturesScreen

        canned = {
            "ok": True,
            "quotes": {
                "BZ=F": {
                    "last": 88.52,
                    "chg_pct": 1.66,
                    "pct5": -0.5,
                    "pct20": 3.1,
                    "volume": 28399.0,
                    "recent": [{
                        "date": "2026-08-14", "open": 86.88, "high": 88.79,
                        "low": 86.44, "close": 88.52, "volume": 28399.0,
                    }],
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            futures = root / "futures"
            futures.mkdir()
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                # Default futures_symbols (["BZ=F"]) applies.
                "plugins": [{"label": "futures", "root": str(futures)}],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch.object(
                    FuturesScreen, "_load", lambda self: canned
                ):
                    async with app.run_test(size=(120, 40)) as pilot:
                        await pilot.pause()
                        pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                        labels = [str(option.prompt) for option in pipeline_list.options]
                        self.assertIn("FUTURES", labels)
                        pipeline_list.focus()
                        pipeline_list.highlighted = labels.index("FUTURES")
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, FuturesScreen)
                        board = app.screen.query_one("#fut-board", DataTable)
                        for _ in range(40):
                            await asyncio.sleep(0.05)
                            await pilot.pause()
                            if board.row_count:
                                break
                        self.assertGreater(board.row_count, 0)
                        row = [str(cell) for cell in board.get_row_at(0)]
                        self.assertEqual(row[0], "BZ=F")
                        self.assertIn("Brent", row[1])
                        self.assertEqual(row[2], "88.52")
                        recent = app.screen.query_one("#fut-recent", DataTable)
                        self.assertGreater(recent.row_count, 0)
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DashboardScreen)

    async def _futures_category_scenario(self) -> None:
        from tui.screens import FuturesScreen

        canned = {
            "ok": True,
            "quotes": {
                "BZ=F": {
                    "last": 88.52, "chg_pct": 1.66, "pct5": -0.5, "pct20": 3.1,
                    "volume": 28399.0,
                    "recent": [{
                        "date": "2026-08-14", "open": 86.88, "high": 88.79,
                        "low": 86.44, "close": 88.52, "volume": 28399.0,
                    }],
                },
            },
            "adapter": {
                "ok": True, "schema": "mktdaily.sgx-board.v1", "asof": "2026-08-14",
                "instruments": [
                    {
                        "code": "MF5F", "group": "Bunker",
                        "name": "Marine Fuel 0.5% FOB Singapore (VLSFO)",
                        "unit": "USD/mt", "front_month": "2026-08",
                        "settle": 734.81, "volume": 10.0, "open_interest": 81.0,
                        "change": 7.08, "change_pct": 0.97,
                        "curve": [{"month": "2026-08", "settle": 734.81},
                                  {"month": "2026-09", "settle": 668.14}],
                        "recent": [{"date": "2026-08-14", "settle": 734.81,
                                    "volume": 10.0, "open_interest": 81.0,
                                    "month": "2026-08"}],
                    },
                    {
                        "code": "GOF", "group": "Bunker",
                        "name": "Gasoil FOB Singapore", "unit": "USD/bbl",
                        "front_month": "2026-08", "settle": 156.11,
                        "volume": 0.0, "open_interest": 0.0,
                        "change": 1.38, "change_pct": 0.89,
                        "curve": [{"month": "2026-08", "settle": 156.11}],
                        "recent": [{"date": "2026-08-14", "settle": 156.11,
                                    "volume": 0.0, "open_interest": 0.0,
                                    "month": "2026-08"}],
                    },
                ],
                "warnings": [],
            },
            "adapter_error": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            futures = root / "futures"
            futures.mkdir()
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                "plugins": [{"label": "futures", "root": str(futures)}],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch.object(FuturesScreen, "_load", lambda self: canned):
                    async with app.run_test(size=(130, 42)) as pilot:
                        await pilot.pause()
                        app.push_screen(FuturesScreen(app.screen.engine, app.screen.config))
                        await pilot.pause()
                        board = app.screen.query_one("#fut-board", DataTable)
                        for _ in range(40):
                            await asyncio.sleep(0.05)
                            await pilot.pause()
                            if board.row_count:
                                break
                        # First category: ENERGY with the Brent row.
                        self.assertEqual(board.row_count, 1)
                        self.assertEqual(str(board.get_row_at(0)[0]), "BZ=F")
                        strip = str(app.screen.query_one("#fut-cats", Static).render())
                        self.assertIn("ENERGY", strip)
                        self.assertIn("BUNKER", strip)
                        # Switch to the BUNKER category: the two fuel rows.
                        await pilot.press("right")
                        await pilot.pause()
                        self.assertEqual(board.row_count, 2)
                        codes = [str(board.get_row_at(i)[0]) for i in range(2)]
                        self.assertEqual(codes, ["MF5F", "GOF"])
                        # Detail panes follow the highlighted fuel row.
                        curve = app.screen.query_one("#fut-curve", DataTable)
                        self.assertEqual(curve.row_count, 2)
                        recent = app.screen.query_one("#fut-recent", DataTable)
                        self.assertEqual(recent.row_count, 1)
                        self.assertEqual(
                            str(recent.get_row_at(0)[1]), "734.81"
                        )
                        # Wrap-around: right again lands back on ENERGY.
                        await pilot.press("right")
                        await pilot.pause()
                        self.assertEqual(str(board.get_row_at(0)[0]), "BZ=F")
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DashboardScreen)

    async def _futures_chart_key_scenario(self) -> None:
        from tui.screens import FuturesScreen

        canned = {
            "ok": True,
            "quotes": {
                "BZ=F": {
                    "last": 88.52, "chg_pct": 1.66, "pct5": -0.5, "pct20": 3.1,
                    "volume": 28399.0,
                    "recent": [{
                        "date": "2026-08-14", "open": 86.88, "high": 88.79,
                        "low": 86.44, "close": 88.52, "volume": 28399.0,
                    }],
                },
            },
            "adapter": {
                "ok": True, "schema": "mktdaily.sgx-board.v1", "asof": "2026-08-14",
                "instruments": [{
                    "code": "MF5F", "group": "Bunker",
                    "name": "Marine Fuel 0.5% FOB Singapore (VLSFO)",
                    "unit": "USD/mt", "front_month": "2026-08",
                    "settle": 734.81, "volume": 10.0, "open_interest": 81.0,
                    "change": 7.08, "change_pct": 0.97,
                    "curve": [{"month": "2026-08", "settle": 734.81}],
                    "recent": [{"date": "2026-08-14", "settle": 734.81,
                                "volume": 10.0, "open_interest": 81.0,
                                "month": "2026-08"}],
                }],
                "warnings": [],
            },
            "adapter_error": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            futures = root / "futures"
            futures.mkdir()
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                "external_bars_root": str(root / "export"),
                "plugins": [{"label": "futures", "root": str(futures)}],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch.object(FuturesScreen, "_load", lambda self: canned):
                    async with app.run_test(size=(130, 42)) as pilot:
                        await pilot.pause()
                        app.push_screen(FuturesScreen(app.screen.engine, app.screen.config))
                        await pilot.pause()
                        board = app.screen.query_one("#fut-board", DataTable)
                        for _ in range(40):
                            await asyncio.sleep(0.05)
                            await pilot.pause()
                            if board.row_count:
                                break
                        opened = []
                        with mock.patch("webbrowser.open",
                                        lambda url: opened.append(url) or True), \
                             mock.patch("tui.chart_server.ensure_running",
                                        return_value=9876):
                            # Exchange-settled row → the SGX: chart symbol.
                            await pilot.press("right")
                            await pilot.pause()
                            await pilot.press("k")
                            for _ in range(40):
                                await asyncio.sleep(0.05)
                                await pilot.pause()
                                if opened:
                                    break
                            self.assertTrue(opened, "k on an SGX row opened nothing")
                            self.assertIn("/chart/SGX:MF5F", opened[-1])
                            # Provider-backed row keeps the plain symbol.
                            await pilot.press("left")
                            await pilot.pause()
                            await pilot.press("k")
                            for _ in range(40):
                                await asyncio.sleep(0.05)
                                await pilot.pause()
                                if len(opened) > 1:
                                    break
                            self.assertIn("/chart/BZ=F", opened[-1])

    async def _shipping_chart_key_scenario(self) -> None:
        board_json = {
            "ok": True,
            "schema": "mktdaily.sgx-board.v1",
            "asof": "2026-08-14",
            "source": "SGX daily settlement file (links.sgx.com derivatives-daily)",
            "instruments": [{
                "code": "CWF", "group": "FFA time charter",
                "name": "Capesize 5TC basket", "unit": "USD/day",
                "front_month": "2026-08", "settle": 39343.0,
                "volume": 847.0, "open_interest": 1000.0,
                "change": 143.0, "change_pct": 0.36,
                "curve": [{"month": "2026-08", "settle": 39343.0}],
                "recent": [{"date": "2026-08-14", "settle": 39343.0}],
            }],
            "warnings": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = root / "fake_adapter.py"
            adapter.write_text(
                "import json\n"
                f"print(json.dumps({board_json!r}))\n",
                encoding="utf-8",
            )
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                "external_bars_root": str(root / "export"),
                "shipping_cmd": f"{sys.executable} {adapter}",
                "plugins": [{"label": "shipping", "root": str(root / "shipping")}],
            }), encoding="utf-8")
            (root / "shipping").mkdir()
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(130, 42)) as pilot:
                    await pilot.pause()
                    pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                    labels = [str(option.prompt) for option in pipeline_list.options]
                    pipeline_list.focus()
                    pipeline_list.highlighted = labels.index("SHIPPING")
                    await pilot.press("enter")
                    await pilot.pause()
                    board = app.screen.query_one("#ship-board", DataTable)
                    for _ in range(40):
                        await asyncio.sleep(0.05)
                        await pilot.pause()
                        if board.row_count:
                            break
                    opened = []
                    with mock.patch("webbrowser.open",
                                    lambda url: opened.append(url) or True), \
                         mock.patch("tui.chart_server.ensure_running",
                                    return_value=9876):
                        await pilot.press("k")
                        for _ in range(40):
                            await asyncio.sleep(0.05)
                            await pilot.pause()
                            if opened:
                                break
                        self.assertTrue(opened, "k on the shipping board opened nothing")
                        self.assertIn("/chart/SGX:CWF", opened[-1])

    async def _shipping_screen_scenario(self) -> None:
        from tui.screens import ShippingScreen

        board_json = {
            "ok": True,
            "schema": "mktdaily.sgx-board.v1",
            "asof": "2026-08-14",
            "source": "SGX daily settlement file (links.sgx.com derivatives-daily)",
            "instruments": [{
                "code": "CWF", "group": "FFA time charter",
                "name": "Capesize 5TC basket", "unit": "USD/day",
                "front_month": "2026-08", "settle": 39343.0,
                "volume": 847.0, "open_interest": 1000.0,
                "change": 143.0, "change_pct": 0.36,
                "curve": [{"month": "2026-08", "settle": 39343.0},
                          {"month": "2026-09", "settle": 39882.0}],
                "recent": [{"date": "2026-08-14", "settle": 39343.0},
                           {"date": "2026-08-13", "settle": 39200.0}],
            }],
            "warnings": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shipping = root / "shipping"
            shipping.mkdir()
            adapter = root / "fake_adapter.py"
            adapter.write_text(
                "import json, sys\n"
                f"print(json.dumps({board_json!r}))\n",
                encoding="utf-8",
            )
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                "shipping_cmd": f"{sys.executable} {adapter}",
                "plugins": [{"label": "shipping", "root": str(shipping)}],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                    labels = [str(option.prompt) for option in pipeline_list.options]
                    self.assertIn("SHIPPING", labels)
                    pipeline_list.focus()
                    pipeline_list.highlighted = labels.index("SHIPPING")
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ShippingScreen)
                    board = app.screen.query_one("#ship-board", DataTable)
                    for _ in range(40):
                        await asyncio.sleep(0.05)
                        await pilot.pause()
                        if board.row_count:
                            break
                    self.assertGreater(board.row_count, 0)
                    row = [str(cell) for cell in board.get_row_at(0)]
                    self.assertEqual(row[1], "CWF")
                    self.assertEqual(row[4], "39,343.00")
                    curve = app.screen.query_one("#ship-curve", DataTable)
                    self.assertEqual(curve.row_count, 2)
                    recent = app.screen.query_one("#ship-recent", DataTable)
                    self.assertEqual(recent.row_count, 2)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

    async def _shipping_spval_scenario(self) -> None:
        from tui.screens import ShippingScreen

        board_json = {
            "ok": True,
            "schema": "mktdaily.sgx-board.v1",
            "asof": "2026-08-14",
            "source": "fake",
            "instruments": [{
                "code": "CWF", "group": "FFA time charter",
                "name": "Capesize 5TC basket", "unit": "USD/day",
                "front_month": "2026-08", "settle": 39343.0,
                "volume": 847.0, "open_interest": 1000.0,
                "change": 143.0, "change_pct": 0.36,
                "curve": [{"month": "2026-08", "settle": 39343.0}],
                "recent": [{"date": "2026-08-14", "settle": 39343.0}],
            }],
            "warnings": [],
        }
        spval_json = {
            "ok": True,
            "schema": "stammtisch.spval-board.v2",
            "asof": "2026-07-26",
            "source": "fake",
            "baseline": {"price": 12000000.0, "scenario": "B", "opex": 5800.0,
                         "util": 345.0, "ffa": 16175.0, "cover": 0.6,
                         "n": 20000, "seed": 42},
            "kpis": {"ret_med": 2.9, "ret_p5": -60.5, "ret_p95": 85.7,
                     "p_loss": 47.3, "es10": -63.0, "irr_med_pct": 0.5,
                     "payback_med_yr": 8.0, "ebitda_y1_med": 1754865.0},
            "grid": [{"key": "A@11M", "scenario": "A", "price": 11000000.0,
                      "ret_med": 4.7, "ret_p5": -70.0, "p_loss": 46.3,
                      "es10": -73.7, "irr_med_pct": 0.8},
                     {"key": "B@12M", "scenario": "B", "price": 12000000.0,
                      "ret_med": 2.9, "ret_p5": -60.5, "p_loss": 47.3,
                      "es10": -63.0, "irr_med_pct": 0.5}],
            "maxbid": {"m1": 0.85, "m2": 1.05, "m3": 1.0, "m4": 0.85,
                       "m5": 0.6, "pim": 0.455175, "base": 18000000.0,
                       "d1": 500000.0, "d2": 2890000.0, "value": 4803150.0},
            "greeks": [{"factor": "TCE ±10%", "down": -23.0, "up": 23.0}],
            "market": {
                "cycle_annual_tc": {"2007": 54135, "2016": 5952, "2026": 16304},
                "route_last24m": {"2026-06-30": 14174, "2026-07-31": 11961},
                "tc_stats": {"med": 13000.0, "max": 83000.0,
                             "max_date": "2007-10-26", "min": 4350.0,
                             "min_date": "2016-02-05"},
                "scrap_premium": {"last": 2.3, "mean": 1.53},
                "vol_by_year": {"2021": 8118, "2026": 3372},
                "ou": {"half_life_weeks": 9.6, "long_run_mean_tce": 8286},
            },
            "risk": {
                "tail_matrix": [
                    {"key": "A@11M", "ret_med": 4.7, "ret_p5": -70.0,
                     "ret_p95": 100.0, "p_loss": 46.3, "es10": -73.7,
                     "irr_med_pct": 0.8},
                    {"key": "D+EXIT@11M", "ret_med": 32.2, "ret_p5": -33.4,
                     "ret_p95": 120.4, "p_loss": 23.7, "es10": -36.5,
                     "irr_med_pct": 5.3}],
                "counterfactual": {"基准($12M, TCE=中位$10k, 残值退出)": -6,
                                   "CF3 无空余就业(利用率345→250天)": -69},
                "sens_matrix": {"12": {"7000": -75, "10000": -6, "13000": 63,
                                       "16000": 132, "20000": 224}},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "shipping").mkdir()
            ffa_adapter = root / "fake_ffa.py"
            ffa_adapter.write_text(
                f"import json\nprint(json.dumps({board_json!r}))\n",
                encoding="utf-8")
            spval_adapter = root / "fake_spval.py"
            spval_adapter.write_text(
                f"import json\nprint(json.dumps({spval_json!r}))\n",
                encoding="utf-8")
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                "shipping_cmd": f"{sys.executable} {ffa_adapter}",
                "spval_cmd": f"{sys.executable} {spval_adapter}",
                "plugins": [{"label": "shipping", "root": str(root / "shipping")}],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                    labels = [str(option.prompt) for option in pipeline_list.options]
                    pipeline_list.focus()
                    pipeline_list.highlighted = labels.index("SHIPPING")
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ShippingScreen)
                    board = app.screen.query_one("#ship-board", DataTable)
                    for _ in range(40):
                        await asyncio.sleep(0.05)
                        await pilot.pause()
                        if board.row_count:
                            break
                    self.assertGreater(board.row_count, 0)
                    header = str(app.screen.query_one("#ship-header", Static).content)
                    self.assertIn("K-line", header)
                    # → S&P VALUATION category: wrap swap + lazy load
                    await pilot.press("right")
                    await pilot.pause()
                    header = str(app.screen.query_one("#ship-header", Static).content)
                    self.assertNotIn("K-line", header)
                    self.assertEqual(
                        app.screen.query_one("#ffa-wrap").styles.display, "none")
                    self.assertEqual(
                        app.screen.query_one("#spval-wrap").styles.display, "block")
                    grid = app.screen.query_one("#spval-grid", DataTable)
                    for _ in range(40):
                        await asyncio.sleep(0.05)
                        await pilot.pause()
                        if grid.row_count:
                            break
                    self.assertEqual(grid.row_count, 2)
                    first = [str(cell) for cell in grid.get_row_at(0)]
                    self.assertEqual(first[0], "A@11M")
                    kpi = app.screen.query_one("#spval-kpi", DataTable)
                    self.assertEqual(kpi.row_count, 8)
                    maxbid = str(app.screen.query_one("#spval-maxbid", Static).content)
                    self.assertIn("4,803,150", maxbid)
                    strip = str(app.screen.query_one("#ship-cats", Static).content)
                    self.assertIn("S&P VALUATION", strip)
                    # → MARKET
                    await pilot.press("right")
                    await asyncio.sleep(0.3)
                    await pilot.pause()
                    self.assertEqual(
                        app.screen.query_one("#mkt-wrap").styles.display, "block")
                    cycle = app.screen.query_one("#mkt-cycle", DataTable)
                    self.assertEqual(cycle.row_count, 3)
                    route = app.screen.query_one("#mkt-route", DataTable)
                    self.assertEqual(route.row_count, 2)
                    # → RISK
                    await pilot.press("right")
                    await asyncio.sleep(0.3)
                    await pilot.pause()
                    self.assertEqual(
                        app.screen.query_one("#risk-wrap").styles.display, "block")
                    tail = app.screen.query_one("#risk-tail", DataTable)
                    self.assertEqual(tail.row_count, 2)
                    sens = app.screen.query_one("#risk-sens", DataTable)
                    self.assertEqual(sens.row_count, 1)
                    # → wraps back to FFA
                    await pilot.press("right")
                    await asyncio.sleep(0.3)
                    await pilot.pause()
                    self.assertEqual(
                        app.screen.query_one("#ffa-wrap").styles.display, "block")
                    # ← wraps to RISK
                    await pilot.press("left")
                    await asyncio.sleep(0.3)
                    await pilot.pause()
                    self.assertEqual(
                        app.screen.query_one("#risk-wrap").styles.display, "block")
                    # → back to FFA
                    await pilot.press("right")
                    await asyncio.sleep(0.3)
                    await pilot.pause()
                    self.assertEqual(
                        app.screen.query_one("#ffa-wrap").styles.display, "block")
                    self.assertEqual(
                        app.screen.query_one("#spval-wrap").styles.display, "none")
                    header = str(app.screen.query_one("#ship-header", Static).content)
                    self.assertIn("K-line", header)

    async def _shipping_fallback_scenario(self) -> None:
        from tui.screens import DomainBrowserScreen

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shipping = root / "shipping"
            shipping.mkdir()
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                # No shipping_cmd: the plugin keeps the directory browser.
                "plugins": [{"label": "shipping", "root": str(shipping)}],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                    labels = [str(option.prompt) for option in pipeline_list.options]
                    pipeline_list.focus()
                    pipeline_list.highlighted = labels.index("SHIPPING")
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DomainBrowserScreen)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

    async def _plugins_click_scenario(self) -> None:
        from tui.polymarket import CryptoScreen

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            futures = root / "futures"
            futures.mkdir()
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                "plugins": [{"label": "futures", "root": str(futures)}],
            }), encoding="utf-8")
            fake_pm = {"ok": True, "markets": [], "error": None}
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch("tui.polymarket.fetch_markets", return_value=fake_pm):
                    async with app.run_test(size=(120, 40)) as pilot:
                        await pilot.pause()
                        pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                        labels = [str(option.prompt) for option in pipeline_list.options]
                        # Pipelines, built-in modules, and configured
                        # plugins share one alphabetical list.
                        self.assertEqual(
                            labels[:3], ["CRYPTO", "ENERGY", "FUTURES"]
                        )
                        self.assertNotIn("FULLSTACK", labels)
                        self.assertIn("SECURITY", labels)
                        # The list draws a top border, so option row N sits
                        # at y=N+1 relative to the widget region; CRYPTO is
                        # the first row.
                        await pilot.click("#pipeline-list", offset=(3, 1))
                        for _ in range(20):
                            await pilot.pause()
                            if not isinstance(app.screen, DashboardScreen):
                                break
                        # Clicking the CRYPTO row must open CRYPTO, not the
                        # row below it.
                        self.assertIsInstance(app.screen, CryptoScreen)
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DashboardScreen)

    async def _quick_click_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as f:
                json.dump({
                    "state_root": os.path.join(tmp, "state"),
                    "data_dir": os.path.join(tmp, "data"),
                }, f)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    quick = app.screen.query_one("#quick-list", OptionList)
                    labels = [str(option.prompt) for option in quick.options]
                    self.assertEqual(labels[1], "[E] EDIT CONFIG")
                    # Second option row + one row of top border = y=2.
                    await pilot.click("#quick-list", offset=(3, 2))
                    for _ in range(20):
                        await pilot.pause()
                        if not isinstance(app.screen, DashboardScreen):
                            break
                    # [E] must open the config screen, not the row below.
                    self.assertIsInstance(app.screen, ConfigScreen)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

    async def _leave_clears_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as f:
                json.dump({"state_root": os.path.join(tmp, "state")}, f)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                    pipeline_list.focus()
                    await pilot.press("down")
                    await pilot.pause()
                    self.assertIsNotNone(pipeline_list.highlighted)
                    await pilot.hover("#pipeline-list")
                    await pilot.pause()
                    await pilot.hover("#run-table")
                    await pilot.pause()
                    # Hover is transient, but keyboard selection remains
                    # authoritative after the pointer leaves the widget.
                    self.assertIsNotNone(pipeline_list.highlighted)

    async def _pipeline_run_scenario(self) -> None:
        from tui.screens import PipelineRunScreen

        class _Result:
            ok = True
            data = {"terminal": "completed", "run_id": "run-1", "detail": ""}

        class _InspectResult:
            ok = True
            data = {"events": []}

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as f:
                json.dump({"state_root": os.path.join(tmp, "state")}, f)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    dashboard = app.screen
                    app.push_screen(PipelineRunScreen(dashboard.driver, dashboard.ai))
                    await pilot.pause()
                    self.assertIsInstance(app.screen, PipelineRunScreen)
                    # Selecting previews only.  The first Enter selects and
                    # moves focus to the explicit Run button; the second
                    # Enter is the confirmation boundary that starts work.
                    with (
                        mock.patch.object(
                            dashboard.driver, "run", return_value=_Result()
                        ) as spy,
                        mock.patch.object(
                            dashboard.driver,
                            "inspect",
                            return_value=_InspectResult(),
                        ) as inspect_spy,
                    ):
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertEqual(spy.call_count, 0)
                        run_button = app.screen.query_one("#pr-run")
                        self.assertTrue(run_button.has_focus)
                        self.assertFalse(run_button.disabled)
                        await pilot.press("enter")
                        for _ in range(80):
                            await asyncio.sleep(0.05)
                            await pilot.pause()
                            if spy.call_count and not app.screen._is_running:
                                break
                        self.assertEqual(spy.call_count, 1)
                        self.assertFalse(app.screen._is_running)
                        self.assertEqual(inspect_spy.call_count, 1)
                        # A queued/repeated activation of the same armed
                        # selection cannot launch a second durable run,
                        # including after a very fast first result.
                        app.screen.action_execute()
                        await pilot.pause()
                        self.assertEqual(spy.call_count, 1)
                        self.assertIsInstance(app.screen, PipelineRunScreen)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

    async def _pipeline_run_selection_isolation_scenario(self) -> None:
        import threading as _threading

        from tui.screens import PipelineRunScreen
        from tui.widgets import EventTimeline, StageFlowWidget

        class _Result:
            ok = True
            data = {"terminal": "completed", "run_id": "run-a", "detail": ""}

        class _InspectResult:
            ok = True
            data = {"events": [{"seq": 1, "type": "run.completed"}]}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pipeline_dir = root / "pipelines"
            pipeline_dir.mkdir()
            (pipeline_dir / "a.json").write_text(json.dumps({
                "schema": "stammtisch.pipeline.v0",
                "id": "a",
                "stages": [{"id": "a-stage", "product": "fake"}],
            }), encoding="utf-8")
            (pipeline_dir / "b.json").write_text(json.dumps({
                "schema": "stammtisch.pipeline.v0",
                "id": "b",
                "stages": [{"id": "b-stage", "product": "fake"}],
            }), encoding="utf-8")
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
            }), encoding="utf-8")
            started = _threading.Event()
            release = _threading.Event()

            def delayed_run(path):
                self.assertTrue(path.endswith("a.json"))
                started.set()
                release.wait(timeout=5)
                return _Result()

            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    dashboard = app.screen
                    dashboard.driver.pipeline_dir = str(pipeline_dir)
                    app.push_screen(PipelineRunScreen(dashboard.driver, dashboard.ai))
                    await pilot.pause()
                    screen = app.screen
                    with (
                        mock.patch.object(dashboard.driver, "run", side_effect=delayed_run) as spy,
                        mock.patch.object(
                            dashboard.driver,
                            "inspect",
                            return_value=_InspectResult(),
                        ) as inspect_spy,
                    ):
                        # Select A, then explicitly run it.
                        await pilot.press("enter")
                        await pilot.press("enter")
                        for _ in range(40):
                            await asyncio.sleep(0.025)
                            await pilot.pause()
                            if started.is_set():
                                break
                        self.assertTrue(started.is_set())

                        # Preview B while A is still running.  B may be
                        # armed, but A's late result must not mark B done or
                        # install A's event log under B.
                        options = screen.query_one("#pipeline-select", OptionList)
                        options.focus()
                        await pilot.press("down")
                        await pilot.press("enter")
                        await pilot.pause()
                        flow = screen.query_one("#pr-stage-flow", StageFlowWidget)
                        self.assertEqual([s["id"] for s in flow.stages], ["b-stage"])
                        self.assertEqual(flow.stage_states, {})

                        release.set()
                        for _ in range(80):
                            await asyncio.sleep(0.025)
                            await pilot.pause()
                            if not screen._is_running:
                                break
                        self.assertFalse(screen._is_running)
                        self.assertEqual(spy.call_count, 1)
                        self.assertEqual(inspect_spy.call_count, 1)
                        self.assertEqual([s["id"] for s in flow.stages], ["b-stage"])
                        self.assertEqual(flow.stage_states, {})
                        self.assertEqual(
                            screen.query_one("#pr-timeline", EventTimeline).events,
                            [],
                        )

    async def _dashboard_status_failure_scenario(self) -> None:
        class _FailedStatus:
            ok = False
            error_message = "event log digest mismatch"
            data = {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    dashboard = app.screen
                    with (
                        mock.patch.object(
                            dashboard.driver,
                            "status",
                            return_value=_FailedStatus(),
                        ) as status_spy,
                        mock.patch.object(
                            dashboard.driver,
                            "get_manifest",
                            side_effect=AssertionError("manifest fallback forbidden"),
                            create=True,
                        ),
                    ):
                        dashboard.action_refresh()
                        await pilot.pause()
                    self.assertEqual(status_spy.call_count, 1)
                    self.assertEqual(
                        dashboard.query_one("#run-table").row_count,
                        0,
                    )
                    message = str(
                        dashboard.query_one("#dash-status", Static).render()
                    )
                    self.assertIn("Run registry unavailable", message)
                    self.assertIn("event log digest mismatch", message)

    async def _run_inspector_snapshot_scenario(self) -> None:
        from tui.screens import RunInspectorScreen
        from tui.widgets import EventTimeline

        class _InspectResult:
            ok = True
            error_message = None
            data = {
                "manifest": {
                    "state": {"code": "completed"},
                    "pipeline": {
                        "id": "fixture",
                        "canonical_sha256": "sha256:" + "a" * 64,
                    },
                    "created_at": "2026-08-16T00:00:00Z",
                    "terminal": {
                        "code": "completed",
                        "at": "2026-08-16T00:01:00Z",
                    },
                },
                "events": [{"seq": 1, "type": "run.completed"}],
                "gates": [{
                    "file": "review.gate.json",
                    "sha256": "sha256:" + "b" * 64,
                    "record": {"gate_id": "review", "decision": "pass"},
                }],
                "receipts": [{
                    "file": "review.0.json",
                    "sha256": "sha256:" + "c" * 64,
                    "receipt": {"schema": "fixture.receipt.v1"},
                }],
            }

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(Path(tmp) / "state"),
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    dashboard = app.screen
                    with (
                        mock.patch.object(
                            dashboard.driver,
                            "inspect",
                            return_value=_InspectResult(),
                        ) as inspect_spy,
                        mock.patch.object(
                            dashboard.driver,
                            "get_manifest",
                            side_effect=AssertionError("direct read forbidden"),
                            create=True,
                        ),
                        mock.patch.object(
                            dashboard.driver,
                            "get_run_events",
                            side_effect=AssertionError("direct read forbidden"),
                            create=True,
                        ),
                        mock.patch.object(
                            dashboard.driver,
                            "get_gate_records",
                            side_effect=AssertionError("direct read forbidden"),
                            create=True,
                        ),
                        mock.patch.object(
                            dashboard.driver,
                            "get_receipts",
                            side_effect=AssertionError("direct read forbidden"),
                            create=True,
                        ),
                    ):
                        app.push_screen(
                            RunInspectorScreen(dashboard.driver, dashboard.ai, "run-1")
                        )
                        for _ in range(80):
                            await asyncio.sleep(0.025)
                            await pilot.pause()
                            if app.screen._snapshot is not None:
                                break
                        screen = app.screen
                        self.assertIsNotNone(screen._snapshot)
                        self.assertEqual(inspect_spy.call_count, 1)
                        self.assertEqual(
                            screen.query_one("#ri-timeline", EventTimeline).events,
                            _InspectResult.data["events"],
                        )
                        receipts = str(
                            screen.query_one("#ri-receipts", Static).render()
                        )
                        self.assertIn("review.0.json", receipts)
                        self.assertIn("fixture.receipt.v1", receipts)
                        self.assertIn(
                            "Verified snapshot",
                            str(screen.query_one("#ri-status", Static).render()),
                        )
                        summary = str(screen.query_one("#ri-summary", Static).render())
                        self.assertIn("State", summary)
                        self.assertIn("completed", summary)
                        self.assertIn("review", summary)

    async def _run_inspector_error_scenario(self) -> None:
        from tui.screens import RunInspectorScreen

        class _InspectResult:
            ok = False
            error_message = "unrecorded receipt digest"
            data = {}

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(Path(tmp) / "state"),
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    dashboard = app.screen
                    with mock.patch.object(
                        dashboard.driver,
                        "inspect",
                        return_value=_InspectResult(),
                    ):
                        app.push_screen(
                            RunInspectorScreen(dashboard.driver, dashboard.ai, "run-bad")
                        )
                        for _ in range(80):
                            await asyncio.sleep(0.025)
                            await pilot.pause()
                            if app.screen._inspect_error is not None:
                                break
                        screen = app.screen
                        self.assertIsNone(screen._snapshot)
                        self.assertEqual(
                            screen._inspect_error,
                            "unrecorded receipt digest",
                        )
                        status = str(
                            screen.query_one("#ri-status", Static).render()
                        )
                        self.assertIn("CORRUPT / UNAVAILABLE", status)
                        screen._run_ai_analysis()
                        await pilot.pause()
                        self.assertFalse(screen._ai_running)

    async def _intake_invalid_config_scenario(self) -> None:
        from tui.screens import DailyIntakeScreen

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as f:
                json.dump({
                    "state_root": os.path.join(tmp, "state"),
                    "intake_cmd": "some-daily-data-product",
                    # Above the driver's ceiling: the config editor accepts
                    # it, IntakeDriver rejects it.
                    "intake_timeout_seconds": 7200,
                    # Keeps the landing view's history index inside tmp.
                    "workspace_root": os.path.join(tmp, "ws"),
                }, f)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    app.push_screen(
                        DailyIntakeScreen(app.screen.config, auto_start=False)
                    )
                    await pilot.pause()
                    screen = app.screen
                    # Must not raise (an action-handler exception exits the
                    # app) and must not wedge the screen in CAPTURING.
                    screen.action_capture()
                    await pilot.pause()
                    self.assertFalse(screen._capture_running)
                    self.assertIsNone(screen._result)
                    text = str(screen.query_one("#intake-text", Static).render())
                    self.assertNotIn("CAPTURING", text)

    async def _intake_session_progress_scenario(self) -> None:
        from tui.screens import DailyIntakeScreen

        started = threading.Event()
        release = threading.Event()
        accepted = SimpleNamespace(
            ok=True,
            error=None,
            envelope={"date": "20260818", "market_counts": {"ashare": 1}, "quality": {"status": "passed", "issues": []}},
            counts={"expected": 1, "succeeded": 1, "failed": 0, "pruned": 0, "canonical_records": 0},
            artifacts={},
        )

        def _slow_run(self, date=None):
            started.set()
            release.wait(timeout=5)
            return accepted

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ws").mkdir()
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                "intake_cmd": "fixture-intake",
                "workspace_root": str(root / "ws"),
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch("tui.intake.IntakeDriver.run", _slow_run):
                    async with app.run_test(size=(120, 40)) as pilot:
                        await pilot.pause()
                        app.push_screen(DailyIntakeScreen(app.screen.config, auto_start=False))
                        await pilot.pause()
                        await pilot.press("r")
                        for _ in range(40):
                            await asyncio.sleep(0.025)
                            await pilot.pause()
                            if started.is_set():
                                break
                        self.assertTrue(started.is_set())
                        body = str(app.screen.query_one("#intake-text", Static).render())
                        self.assertIn("CAPTURING", body)
                        self.assertIn("daily-intake", body)
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DashboardScreen)
                        titles = []
                        table = app.screen.query_one("#run-table", DataTable)
                        for index in range(table.row_count):
                            titles.append(str(table.get_row_at(index)[0]))
                        self.assertTrue(any("daily-intake" in title for title in titles))
                        release.set()
                        for _ in range(40):
                            await asyncio.sleep(0.025)
                            await pilot.pause()
                            if not app.intake_supervisor.capturing:
                                break
                        self.assertFalse(app.intake_supervisor.capturing)
                        self.assertTrue(app.intake_supervisor.result.ok)

    async def _chat_page_scenario(self) -> None:
        from tui.deepseek import DeepSeekDriver
        from tui.screens import ChatScreen

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as f:
                json.dump({"state_root": os.path.join(tmp, "state")}, f)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    app.push_screen(ChatScreen(DeepSeekDriver(api_key="sk-test")))
                    await pilot.pause()
                    screen = app.screen
                    # The input box owns focus; PgDn must still route to the
                    # chat log scroll action advertised in the header.
                    with mock.patch.object(
                        screen, "action_scroll_down", wraps=screen.action_scroll_down
                    ) as spy:
                        await pilot.press("pagedown")
                        await pilot.pause()
                        self.assertEqual(spy.call_count, 1)

    async def _dashboard_delete_scenario(self) -> None:
        class _Status:
            ok = True
            error_message = None
            data = {
                "runs": [{
                    "run_id": "01a012e6-bb8c-75f3-8597-c55291876dc5",
                    "pipeline_id": "stock-quinte-local",
                    "state": "blocked",
                    "created_at": "2026-08-18T03:25:06Z",
                }]
            }

        class _Deleted:
            ok = True
            error_message = None
            data = {"removed": True}

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({"state_root": os.path.join(tmp, "state")}, handle)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    dashboard = app.screen
                    with mock.patch.object(dashboard.driver, "status", return_value=_Status()):
                        dashboard.action_refresh()
                        for _ in range(40):
                            await asyncio.sleep(0.025)
                            await pilot.pause()
                            if dashboard.query_one("#run-table").row_count:
                                break
                    table = dashboard.query_one("#run-table")
                    self.assertEqual(table.row_count, 1)
                    title = str(table.get_row_at(0)[0])
                    when = str(table.get_row_at(0)[1])
                    self.assertIn("stock-quinte-local", title)
                    self.assertIn("2026-08-18 03:25", when)
                    dashboard.query_one("#run-table").focus()
                    with mock.patch.object(dashboard.driver, "delete", return_value=_Deleted()) as spy:
                        await pilot.press("d")
                        await pilot.pause()
                        from tui.screens import ConfirmScreen
                        self.assertIsInstance(app.screen, ConfirmScreen)
                        await pilot.press("y")
                        for _ in range(40):
                            await asyncio.sleep(0.025)
                            await pilot.pause()
                            if spy.call_count:
                                break
                    self.assertEqual(spy.call_count, 1)
                    self.assertEqual(
                        spy.call_args[0][0],
                        "01a012e6-bb8c-75f3-8597-c55291876dc5",
                    )

    async def _intake_rows_delete_scenario(self) -> None:
        from tui.intake_job import (
            empty_session,
            save_session,
            session_path,
            supervisor_for,
        )
        from tui.screens import ConfirmScreen

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({
                    "state_root": os.path.join(tmp, "state"),
                    "workspace_root": str(workspace),
                }, handle)
            # A session file left `capturing` on disk by an app that died
            # mid-capture, plus one finished session.
            stale = empty_session(workspace, "20260818")
            save_session(stale)
            finished = empty_session(workspace, "20260818")
            finished["state"] = "rejected"
            save_session(finished)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    dashboard = app.screen
                    rows = {row["key"]: row for row in dashboard._intake_session_rows()}
                    self.assertEqual(
                        rows[f"intake:{stale['id']}"]["state"],
                        "interrupted",
                        "an ownerless capturing file must not render as live",
                    )
                    self.assertEqual(rows[f"intake:{finished['id']}"]["state"], "rejected")

                    # The supervisor's live session still shows capturing.
                    job = supervisor_for(app)
                    live = empty_session(workspace, "20260819")
                    save_session(live)
                    with job._lock:
                        job.active = live
                    rows = {row["key"]: row for row in dashboard._intake_session_rows()}
                    self.assertEqual(rows[f"intake:{live['id']}"]["state"], "capturing")

                    # Deleting the live capture is refused, file and memory
                    # intact, no confirm dialog.
                    dashboard._delete_intake_session(str(live["id"]))
                    await pilot.pause()
                    self.assertFalse(isinstance(app.screen, ConfirmScreen))
                    self.assertTrue(session_path(workspace, str(live["id"])).exists())
                    self.assertEqual(job.active.get("id"), live["id"])

                    # Deleting a finished session removes the file and the
                    # in-memory copy through the supervisor's lock.
                    dashboard._delete_intake_session(str(finished["id"]))
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ConfirmScreen)
                    await pilot.press("y")
                    await pilot.pause()
                    self.assertFalse(
                        session_path(workspace, str(finished["id"])).exists()
                    )
                    self.assertFalse(job.forget(str(finished["id"])))
                    # The live capture was never clobbered by that delete.
                    self.assertEqual(job.active.get("id"), live["id"])
                    self.assertTrue(job.is_capturing_session(str(live["id"])))

    async def _dashboard_shift_select_scenario(self) -> None:
        class _Status:
            ok = True
            error_message = None
            data = {
                "runs": [
                    {
                        "run_id": f"01a012e6-bb8c-75f3-8597-c55291876dc{i}",
                        "pipeline_id": "stock-quinte-local",
                        "state": "blocked",
                        "created_at": f"2026-08-18T03:2{i}:06Z",
                    }
                    for i in range(3)
                ]
            }

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({"state_root": os.path.join(tmp, "state")}, handle)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    dashboard = app.screen
                    with mock.patch.object(dashboard.driver, "status", return_value=_Status()):
                        dashboard.action_refresh()
                        for _ in range(40):
                            await asyncio.sleep(0.025)
                            await pilot.pause()
                            if dashboard.query_one("#run-table").row_count == 3:
                                break
                    rows = dashboard._registry_rows(dashboard._runs)
                    first = rows[0]["key"]
                    last = rows[2]["key"]
                    dashboard.on_registry_row_click(first, shift=False)
                    dashboard.on_registry_row_click(last, shift=True)
                    self.assertEqual(dashboard._checked, {item["key"] for item in rows})
                    dashboard.query_one("#run-table").move_cursor(row=0)
                    dashboard.on_registry_cursor_move()
                    self.assertEqual(dashboard._checked, {first})
                    dashboard.on_registry_row_click(first, shift=False)
                    dashboard.on_registry_row_click(last, shift=True)
                    dashboard.action_check_all()
                    self.assertEqual(dashboard._checked, set())
                    dashboard.action_check_all()
                    self.assertEqual(len(dashboard._checked), 3)

    async def _security_cache_scenario(self) -> None:
        from tui.screens import SecurityScreen

        loads = {"n": 0}

        def _load(self):
            loads["n"] += 1
            return {
                "ok": True,
                "quotes": {
                    "BTU": {
                        "last": 25.0, "chg_pct": 1.0, "pct5": 2.0, "pct20": 3.0,
                        "volume": 1000.0,
                        "recent": [{
                            "date": "2026-08-18", "open": 24.0, "high": 25.0,
                            "low": 24.0, "close": 25.0, "volume": 1000.0,
                        }],
                    }
                },
            }

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({
                    "state_root": os.path.join(tmp, "state"),
                    "security_symbols": ["BTU"],
                }, handle)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch.object(SecurityScreen, "_load", _load):
                    async with app.run_test(size=(120, 40)) as pilot:
                        await pilot.pause()
                        pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                        labels = [str(option.prompt) for option in pipeline_list.options]
                        pipeline_list.focus()
                        pipeline_list.highlighted = labels.index("SECURITY")
                        await pilot.press("enter")
                        await pilot.pause()
                        for _ in range(40):
                            await asyncio.sleep(0.025)
                            await pilot.pause()
                            if app.screen.query_one("#sec-board", DataTable).row_count:
                                break
                        self.assertEqual(loads["n"], 1)
                        await pilot.press("escape")
                        await pilot.pause()
                        pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                        pipeline_list.focus()
                        pipeline_list.highlighted = labels.index("SECURITY")
                        await pilot.press("enter")
                        await pilot.pause()
                        board = app.screen.query_one("#sec-board", DataTable)
                        self.assertGreater(board.row_count, 0)
                        self.assertEqual(loads["n"], 1)
                        status = str(app.screen.query_one("#sec-fetch", Static).render())
                        self.assertIn("cached", status)

    async def _brief_scenario(self) -> None:
        from tui.brief import BriefScreen, load_daily_path

        with tempfile.TemporaryDirectory() as tmp:
            report_json = Path(tmp) / "report.json"
            report_json.write_text(json.dumps({
                "date": "20260814",
                "model": "fixture",
                "brief": [{"text": "\u4e2d\u6587\u65e5\u62a5\u6458\u8981", "sources": ["Fixture"]}],
                "markets": {"ashare": [], "hk": [], "us": [], "crypto": []},
                "notes": [],
            }, ensure_ascii=False), encoding="utf-8")
            doc = load_daily_path(report_json, expected_date="20260814")
            self.assertTrue(doc.get("ok"), doc.get("error"))
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as f:
                json.dump({
                    "state_root": os.path.join(tmp, "state"),
                    "data_dir": os.path.join(tmp, "data"),
                }, f)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    app.push_screen(BriefScreen(doc))
                    await pilot.pause()
                    self.assertIsInstance(app.screen, BriefScreen)
                    body = str(app.screen.query_one("#brief-text", Static).render())
                    self.assertIn("Fin-daily", body)
                    self.assertNotIn("lightweight-charts", body.lower())
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

    async def _daily_intake_scenario(self) -> None:
        from types import SimpleNamespace

        from tui.brief import BriefScreen, SentimentScreen
        from tui.screens import DailyIntakeScreen

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_json = root / "report.json"
            report_html = root / "report.html"
            evidence_manifest = root / "evidence.json"
            canonical_dataset = root / "canonical.json"
            report_json.write_text(json.dumps({
                "date": "20260814",
                "model": "fixture",
                "brief": [{"text": "\u4e2d\u6587\u65e5\u62a5", "sources": ["Fixture"]}],
                "markets": {"ashare": [], "hk": [], "us": [], "crypto": []},
                "notes": [],
            }, ensure_ascii=False), encoding="utf-8")
            report_html.write_text("<!doctype html><html></html>", encoding="utf-8")
            evidence_manifest.write_text(json.dumps({
                "captures": [
                    {"source": "fixture_us", "status": "succeeded"},
                    {"source": "fixture_hk", "status": "succeeded"},
                    {"source": "offline_source", "status": "failed"},
                    {"source": "retired_source", "status": "pruned"},
                ],
            }), encoding="utf-8")
            canonical_dataset.write_text(json.dumps({
                "records": [
                    {
                        "market": "us",
                        "title": "Fed [holds] rates as source reports",
                    },
                    {
                        "market": "hk",
                        "title": "\u6e2f\u80a1\u6536\u5e02\u539f\u6587\u6807\u9898",
                    },
                ],
            }, ensure_ascii=False), encoding="utf-8")
            artifacts = {
                "evidence_manifest": evidence_manifest,
                "canonical_dataset": canonical_dataset,
                "report_json": report_json,
                "report_html": report_html,
            }
            accepted = SimpleNamespace(
                ok=True,
                error=None,
                envelope={
                    "date": "20260814",
                    "market_counts": {"ashare": 1, "hk": 1, "us": 1},
                    "quality": {
                        "status": "degraded",
                        "complete": False,
                        "issues": [
                            "1 source capture(s) failed",
                            "1 source capture(s) were pruned",
                        ],
                    },
                    "artifacts": {
                        "evidence_manifest": {"sha256": "a" * 64},
                        "canonical_dataset": {"sha256": "b" * 64},
                        "report_json": {"sha256": "c" * 64},
                        "report_html": {"sha256": "d" * 64},
                    },
                },
                counts={
                    "expected": 4, "succeeded": 2, "failed": 1, "pruned": 1,
                    "canonical_records": 2,
                },
                artifacts=artifacts,
            )
            # History fixtures: one intake-native run and one legacy import,
            # so the landing view has a newest report to surface.
            history_run = root / "runs" / "20260813T010203Z-fixtur01"
            history_run.mkdir(parents=True)
            history_json = history_run / "fin-daily-20260813.json"
            history_json.write_text(json.dumps({
                "schema": "stammtisch.daily-report.v1",
                "date": "20260813",
                "run_id": "20260813T010203Z-fixtur01",
                "brief": [{"text": "中文旧报摘要", "sources": ["Fixture"]}],
                "markets": {"ashare": [{
                    "title": "旧报中文标题",
                    "url": "https://example.test/a",
                    "summary": "旧报摘要",
                    "sources": ["Fixture"],
                }]},
                "market_counts": {"ashare": 1},
            }, ensure_ascii=False), encoding="utf-8")
            legacy_output = root / "legacy" / "20260812" / "output"
            legacy_output.mkdir(parents=True)
            (legacy_output / "fin-daily-20260812.refined.json").write_text(json.dumps({
                "date": "20260812",
                "model": "fixture",
                "brief": [],
                "markets": {"ashare": []},
                "notes": [],
            }, ensure_ascii=False), encoding="utf-8")
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                "data_dir": str(root / "data"),
                "intake_cmd": "fixture-intake",
                "workspace_root": str(root),
                "reports_root": str(root / "legacy"),
            }), encoding="utf-8")

            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch("tui.intake.IntakeDriver.run", return_value=accepted) as run_spy:
                    async with app.run_test(size=(120, 40)) as pilot:
                        await pilot.pause()
                        app.screen.action_open_intake()
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DailyIntakeScreen)
                        self.assertTrue(app.config.intake_argv)
                        # Opening the screen must never capture on its own:
                        # the landing view surfaces the newest indexed report.
                        self.assertEqual(
                            run_spy.call_count,
                            0,
                            "mounting DailyIntakeScreen must not auto-start a capture",
                        )
                        landing = str(app.screen.query_one("#intake-text", Static).render())
                        self.assertIn("Status: READY", landing)
                        self.assertIn("Latest report: 2026-08-13", landing)
                        # Enter opens the indexed latest report (no capture needed).
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, BriefScreen)
                        self.assertEqual(app.screen.doc["json_path"], str(history_json))
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DailyIntakeScreen)
                        # R is the explicit capture trigger.
                        await pilot.press("r")
                        for _ in range(20):
                            await asyncio.sleep(0.05)
                            await pilot.pause()
                            if "Status: ACCEPTED" in str(
                                app.screen.query_one("#intake-text", Static).render()
                            ):
                                break
                        self.assertEqual(
                            run_spy.call_count,
                            1,
                            "pressing R must trigger exactly one capture",
                        )
                        self.assertIsNotNone(
                            app.screen._result,
                            f"running={app.screen._capture_running} calls={run_spy.call_count} "
                            f"delivery={getattr(app.screen, '_delivery_error', None)}",
                        )
                        self.assertTrue(app.screen._result.ok, app.screen._result.error)
                        body = str(app.screen.query_one("#intake-text", Static).render())
                        self.assertIn("Status: ACCEPTED", body)
                        self.assertIn("Quality gate: DEGRADED", body)
                        self.assertIn("1 source capture(s) failed", body)
                        self.assertIn("Failed sources: offline_source", body)
                        self.assertIn("Pruned sources: retired_source", body)
                        self.assertIn("Sources: 2/4", body)
                        self.assertIn("Fed [holds] rates as source reports", body)
                        self.assertIn("\u6e2f\u80a1\u6536\u5e02\u539f\u6587\u6807\u9898", body)
                        self.assertIn("canonical_dataset", body)
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, BriefScreen)
                        # S must consume the exact report graph just verified
                        # by IntakeDriver, not rediscover a parallel legacy
                        # report tree by filename.
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DailyIntakeScreen)
                        await pilot.press("escape")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DashboardScreen)
                        app.screen.action_open_sentiment()
                        await pilot.pause()
                        self.assertIsInstance(app.screen, SentimentScreen)
                        self.assertEqual(
                            app.screen.doc["json_path"], str(report_json)
                        )
                        self.assertIs(app.last_daily_intake_result, accepted)

    async def _daily_intake_rejected_scenario(self) -> None:
        from types import SimpleNamespace

        from tui.screens import DailyIntakeScreen

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence_manifest = root / "evidence.json"
            canonical_dataset = root / "canonical.json"
            evidence_manifest.write_text(json.dumps({
                "captures": [
                    {"source": "unavailable_us", "status": "failed"},
                    {"source": "retired_us", "status": "pruned"},
                ],
            }), encoding="utf-8")
            canonical_dataset.write_text(
                json.dumps({"records": []}), encoding="utf-8"
            )
            rejected = SimpleNamespace(
                ok=False,
                error=(
                    "daily intake quality gate rejected the session: "
                    "selected market session produced no canonical records: us"
                ),
                envelope={
                    "date": "20260814",
                    "session_markets": ["us"],
                    "quality": {
                        "status": "failed",
                        "issues": [
                            "selected market session produced no canonical records: us"
                        ],
                    },
                },
                counts={"expected": 2, "succeeded": 0, "failed": 1, "pruned": 1},
                artifacts={
                    "evidence_manifest": evidence_manifest,
                    "canonical_dataset": canonical_dataset,
                },
            )
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                "data_dir": str(root / "data"),
                "intake_cmd": "fixture-intake",
                "workspace_root": str(root),
                "reports_root": str(root / "legacy"),
            }), encoding="utf-8")

            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                with mock.patch("tui.intake.IntakeDriver.run", return_value=rejected):
                    async with app.run_test(size=(120, 40)) as pilot:
                        await pilot.pause()
                        app.screen.action_open_intake()
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DailyIntakeScreen)
                        # No auto-capture on open; R is the explicit trigger.
                        await pilot.press("r")
                        for _ in range(20):
                            await asyncio.sleep(0.05)
                            await pilot.pause()
                            if "Status: REJECTED" in str(
                                app.screen.query_one("#intake-text", Static).render()
                            ):
                                break
                        self.assertIsInstance(app.screen, DailyIntakeScreen)
                        body = str(app.screen.query_one("#intake-text", Static).render())
                        self.assertIn("Status: REJECTED", body)
                        self.assertIn("Market session: us", body)
                        self.assertIn("Failed sources: unavailable_us", body)
                        self.assertIn("Pruned sources: retired_us", body)
                        self.assertIn("No report JSON or HTML was published.", body)
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, DailyIntakeScreen)

    async def _report_history_scenario(self) -> None:
        from tui.brief import BriefScreen
        from tui.screens import ReportHistoryScreen

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            run_dir = workspace / "runs" / "20260817T094921Z-a1b2c3d4"
            run_dir.mkdir(parents=True)
            report_json = run_dir / "fin-daily-20260817.json"
            report_json.write_text(json.dumps({
                "schema": "stammtisch.daily-report.v1",
                "date": "20260817",
                "run_id": "20260817T094921Z-a1b2c3d4",
                "brief": [{"text": "中文日报摘要", "sources": ["Fixture"]}],
                "markets": {"ashare": [{
                    "title": "中文源标题",
                    "url": "https://example.test/a",
                    "summary": "摘要",
                    "sources": ["Fixture"],
                }], "hk": []},
                "market_counts": {"ashare": 1},
                "intake": {"expected": 14, "succeeded": 10},
            }, ensure_ascii=False), encoding="utf-8")
            legacy_output = root / "legacy" / "20260814" / "output"
            legacy_output.mkdir(parents=True)
            (legacy_output / "fin-daily-20260814.refined.json").write_text(json.dumps({
                "date": "20260814",
                "model": "fixture",
                "brief": [],
                "markets": {"ashare": []},
                "notes": [],
            }, ensure_ascii=False), encoding="utf-8")
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps({
                "state_root": str(root / "state"),
                "data_dir": str(root / "data"),
                "workspace_root": str(workspace),
                "reports_root": str(root / "legacy"),
            }), encoding="utf-8")

            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": str(cfg_path)}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    app.screen.action_open_history()
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ReportHistoryScreen)
                    listing = app.screen.query_one("#history-list", OptionList)
                    labels = [str(option.prompt) for option in listing.options]
                    self.assertEqual(len(labels), 2)
                    # Newest first; intake and legacy origins are both visible.
                    self.assertIn("2026-08-17", labels[0])
                    self.assertIn("intake", labels[0])
                    self.assertIn("ashare 1", labels[0])
                    self.assertIn("2026-08-14", labels[1])
                    self.assertIn("legacy", labels[1])
                    listing.focus()
                    listing.highlighted = 0
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, BriefScreen)
                    self.assertEqual(app.screen.doc["json_path"], str(report_json))
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ReportHistoryScreen)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

    async def _sentiment_scenario(self) -> None:
        from tui.brief import SentimentScreen

        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp) / "legacy-reports"
            output = reports / "20260814" / "output"
            output.mkdir(parents=True)
            (output / "fin-daily-20260814.refined.json").write_text(json.dumps({
                "date": "20260814",
                "brief": [{"text": "Fixture market brief", "sources": ["Fixture"]}],
                "markets": {
                    "ashare": [{"title": "Fixture A-share headline", "url": "https://example.test/a"}],
                    "hk": [], "us": [], "crypto": [],
                },
                "notes": [],
            }), encoding="utf-8")
            # A newer intake-native run in the workspace: S must prefer it
            # over the older legacy tree (intake reports never land there).
            workspace = Path(tmp) / "workspace"
            run_dir = workspace / "runs" / "20260815T010203Z-fixtur02"
            run_dir.mkdir(parents=True)
            intake_json = run_dir / "fin-daily-20260815.json"
            intake_json.write_text(json.dumps({
                "schema": "stammtisch.daily-report.v1",
                "date": "20260815",
                "run_id": "20260815T010203Z-fixtur02",
                "brief": [{"text": "Intake market brief", "sources": ["Fixture"]}],
                "markets": {
                    "ashare": [{"title": "Intake A-share headline", "url": "https://example.test/b"}],
                    "hk": [], "us": [], "crypto": [],
                },
                "market_counts": {"ashare": 1},
            }), encoding="utf-8")
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as f:
                json.dump({
                    "state_root": os.path.join(tmp, "state"),
                    "data_dir": os.path.join(tmp, "data"),
                    "reports_root": str(reports),
                    "workspace_root": str(workspace),
                    "recent_symbols": ["600098.SS"],
                }, f)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    app.screen.action_open_sentiment()
                    await pilot.pause()
                    self.assertIsInstance(app.screen, SentimentScreen)
                    self.assertEqual(app.screen.symbol, "")
                    self.assertEqual(app.screen.doc["json_path"], str(intake_json))
                    body = str(app.screen.query_one("#sent-text", Static).render())
                    self.assertIn("MARKET SENTIMENT", body)
                    self.assertNotIn("\u6807\u7684 600098", body)
                    self.assertNotIn("\u5f53\u65e5\u65e0\u8be5\u6807\u7684\u76f8\u5173\u6807\u9898", body)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

    async def _pipeline_view_scenario(self) -> None:
        from textual.widgets import TextArea

        from tui.screens import PipelineViewScreen

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as f:
                json.dump({
                    "state_root": os.path.join(tmp, "state"),
                    "data_dir": os.path.join(tmp, "data"),
                }, f)
            spec = os.path.join(tmp, "spec.json")
            with open(spec, "w") as f:
                f.write('{"schema": "stammtisch.pipeline.v0", "id": "t-pipe", '
                        '"doctrine": {"pack": "galahad"}, "stages": []}')

            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    driver = app.screen.driver
                    app.push_screen(PipelineViewScreen(driver, spec))
                    await pilot.pause()
                    self.assertIsInstance(app.screen, PipelineViewScreen)
                    summary = str(app.screen.query_one("#pe-area", Static).render())
                    # Rendered summary, never a raw JSON dump.
                    self.assertIn("t-pipe", summary)
                    self.assertIn("galahad", summary)
                    self.assertNotIn('"stages"', summary)

                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

    async def _confirm_scenario(self) -> None:
        import tempfile
        from tui.screens import ConfirmScreen, DashboardScreen

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as f:
                json.dump({
                    "state_root": os.path.join(tmp, "state"),
                    "data_dir": os.path.join(tmp, "data"),
                }, f)

            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    calls: list = []

                    # Keyboard: y confirms.
                    app.push_screen(ConfirmScreen("  Test?", lambda: calls.append("y")))
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ConfirmScreen)
                    await pilot.press("y")
                    await pilot.pause()
                    self.assertEqual(calls, ["y"])
                    self.assertIsInstance(app.screen, DashboardScreen)

                    # Mouse: clicking the confirm button confirms.
                    app.push_screen(ConfirmScreen("  Test?", lambda: calls.append("click")))
                    await pilot.pause()
                    await pilot.click("#confirm-yes")
                    await pilot.pause()
                    self.assertEqual(calls, ["y", "click"])
                    self.assertIsInstance(app.screen, DashboardScreen)

                    # Keyboard: n cancels without firing the callback.
                    app.push_screen(ConfirmScreen("  Test?", lambda: calls.append("no")))
                    await pilot.pause()
                    await pilot.press("n")
                    await pilot.pause()
                    self.assertEqual(len(calls), 2, "cancel must not fire the callback")
                    self.assertIsInstance(app.screen, DashboardScreen)

    async def _scenario(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as f:
                json.dump({
                    "state_root": os.path.join(tmp, "state"),
                    "data_dir": os.path.join(tmp, "data"),
                }, f)

            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(140, 44)) as pilot:
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

                    # Sidebar: pipelines + domains in the middle Plugins list,
                    # general utilities in the bottom Quick Start list.
                    pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                    option_labels = [str(option.prompt) for option in pipeline_list.options]
                    self.assertIn("CRYPTO", option_labels)

                    quick = app.screen.query_one("#quick-list", OptionList)
                    labels = [str(option.prompt) for option in quick.options]
                    self.assertEqual(
                        labels,
                        ["[A] ASK GALAHAD", "[E] EDIT CONFIG"],
                    )

                    # Init stays a dashboard action (CLI-first recovery),
                    # never a menu slot or a binding.
                    driver = app.screen.driver
                    with mock.patch.object(driver, "init", return_value=_ok_result("/state")) as spy:
                        app.screen.action_init_state()
                        await pilot.pause()
                        self.assertEqual(spy.call_count, 1)

                    # EDIT CONFIG is the second (and last) general row.
                    quick.focus()
                    quick.highlighted = 1
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ConfigScreen)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

                    # [E] opens the config editor screen.
                    await pilot.press("e")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, ConfigScreen)

                    # Fill in a key and save with Ctrl+S.
                    key_input = app.screen.query_one("#cfg-key", Input)
                    key_input.value = "sk-test-1234567890"
                    await pilot.press("ctrl+s")
                    await pilot.pause()

                    # Back on the dashboard, config persisted and hot-applied.
                    self.assertIsInstance(app.screen, DashboardScreen)
                    self.assertTrue(app.ai.available)
                    self.assertEqual(app.ai.api_key, "sk-test-1234567890")
                    with open(cfg_path) as f:
                        saved = json.load(f)
                    self.assertEqual(saved["deepseek_api_key"], "sk-test-1234567890")

                    # ConfigScreen keeps other settings intact on save.
                    self.assertEqual(saved["state_root"], os.path.join(tmp, "state"))

                    # Selecting the security example opens the security
                    # market-zone board, never a JSON dump.
                    from tui.screens import SecurityScreen
                    pipeline_list = app.screen.query_one("#pipeline-list", OptionList)
                    pipeline_list.focus()
                    pipeline_list.highlighted = option_labels.index("SECURITY")
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, SecurityScreen)
                    app.screen.query_one("#sec-board", DataTable)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

                    # E binding documented in help screen.
                    await pilot.press("?")
                    await pilot.pause()
                    from tui.screens import HelpScreen
                    self.assertIsInstance(app.screen, HelpScreen)
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

                    # M is unbound now: pressing it must not open anything.
                    await pilot.press("m")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)

                    # C opens the Crypto module (Polymarket tape, no network).
                    from tui.polymarket import CryptoScreen

                    fake_pm = {
                        "ok": True,
                        "markets": [{
                            "id": "1",
                            "question": "Will BTC close above 100k?",
                            "slug": "btc-100k",
                            "yes": 0.42,
                            "volume24hr": 12500.0,
                            "volume": 1e6,
                            "end": "2026-12-31",
                            "fee_rate": 0.07,
                            "category": "Crypto",
                        }],
                        "error": None,
                    }
                    with mock.patch("tui.polymarket.fetch_markets", return_value=fake_pm):
                        # CRYPTO row in the Plugins list opens the module.
                        pipeline_list.highlighted = option_labels.index("CRYPTO")
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertIsInstance(app.screen, CryptoScreen)
                        body = str(app.screen.query_one("#pm-detail", Static).render())
                        self.assertIn("Read-only", body)
                        await pilot.press("escape")
                        await pilot.pause()
                    self.assertIsInstance(app.screen, DashboardScreen)


class _ok_result:
    def __init__(self, state_root: str):
        self.ok = True
        self.data = {"state_root": state_root}
        self.error_message = None


class ChatScreenTest(unittest.TestCase):
    def test_chat_input_multiline_and_submit_keys(self) -> None:
        asyncio.run(self._chat_input_scenario())

    async def _chat_input_scenario(self) -> None:
        from tui.deepseek import ChatResponse
        from tui.screens import ChatInput, ChatScreen

        class _FakeAI:
            available = True

            def chat(self, query, context=None):
                return ChatResponse(content=f"echo:{query}")

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({"state_root": os.path.join(tmp, "state")}, handle)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(100, 30)) as pilot:
                    app.push_screen(ChatScreen(_FakeAI()))
                    await pilot.pause()
                    box = app.screen.query_one("#chat-input", ChatInput)
                    # multi-line content auto-grows and submits as ONE message
                    box.text = "第一行\n第二行\n第三行"
                    await pilot.pause()
                    self.assertEqual(getattr(box.styles.height, "value", box.styles.height), 5)
                    await pilot.press("enter")
                    await asyncio.sleep(0.5)
                    await pilot.pause()
                    self.assertIn("USER: 第一行\n第二行\n第三行", app.screen._chat_log)
                    self.assertEqual(box.text, "")
                    self.assertEqual(getattr(box.styles.height, "value", box.styles.height), 3)
                    # ctrl+j inserts a newline WITHOUT submitting
                    log_before = app.screen._chat_log
                    await pilot.press("ctrl+j")
                    await pilot.pause()
                    self.assertEqual(box.text, "\n")
                    self.assertEqual(app.screen._chat_log, log_before)

    def test_chat_thinking_indicator_replaces_with_answer(self) -> None:
        asyncio.run(self._chat_thinking_scenario())

    def test_chat_serializes_submissions_and_thinking_state(self) -> None:
        asyncio.run(self._chat_serialization_scenario())

    async def _chat_thinking_scenario(self) -> None:
        import time as _time
        from tui.deepseek import ChatResponse
        from tui.screens import ChatInput, ChatScreen

        class _SlowAI:
            available = True

            def chat(self, query, context=None):
                _time.sleep(0.6)
                return ChatResponse(content="done", reasoning_content="先拆解证据再下结论")

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({"state_root": os.path.join(tmp, "state")}, handle)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(100, 30)) as pilot:
                    app.push_screen(ChatScreen(_SlowAI()))
                    await pilot.pause()
                    box = app.screen.query_one("#chat-input", ChatInput)
                    box.text = "hello"
                    await pilot.press("enter")
                    await asyncio.sleep(0.25)
                    self.assertIn("thinking", app.screen._pending or "")
                    await asyncio.sleep(0.8)
                    await pilot.pause()
                    self.assertIsNone(app.screen._pending)
                    self.assertNotIn("先拆解证据再下结论", app.screen._chat_log)
                    self.assertRegex(app.screen._chat_log, r"·\s+\d+s")
                    self.assertIn("GALAHAD: done", app.screen._chat_log)

    async def _chat_serialization_scenario(self) -> None:
        import threading as _threading

        from tui.deepseek import ChatResponse
        from tui.screens import ChatInput, ChatScreen

        class _BlockingAI:
            available = True

            def __init__(self):
                self.calls = []
                self.first_started = _threading.Event()
                self.release_first = _threading.Event()

            def chat(self, query, context=None):
                self.calls.append(query)
                if query == "first":
                    self.first_started.set()
                    self.release_first.wait(timeout=5)
                return ChatResponse(
                    content=f"answer:{query}",
                    reasoning_content=f"trace:{query}",
                )

        ai = _BlockingAI()
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({"state_root": os.path.join(tmp, "state")}, handle)
            with mock.patch.dict(
                os.environ,
                {"STAMMTISCH_CONFIG": cfg_path},
            ):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(100, 30)) as pilot:
                    app.push_screen(ChatScreen(ai))
                    await pilot.pause()
                    screen = app.screen
                    box = screen.query_one("#chat-input", ChatInput)
                    self.assertTrue(screen._submit("first"))
                    for _ in range(40):
                        await asyncio.sleep(0.025)
                        if ai.first_started.is_set():
                            break
                    self.assertTrue(ai.first_started.is_set())
                    first_pending = screen._pending
                    self.assertFalse(screen._submit("second"))
                    self.assertEqual(ai.calls, ["first"])
                    self.assertEqual(screen._pending, first_pending)
                    self.assertTrue(box.disabled)

                    ai.release_first.set()
                    for _ in range(80):
                        await asyncio.sleep(0.025)
                        await pilot.pause()
                        if screen._active_request is None:
                            break
                    self.assertIsNone(screen._active_request)
                    self.assertIsNone(screen._pending)
                    self.assertFalse(box.disabled)
                    self.assertNotIn("trace:first", screen._chat_log)
                    self.assertIn("GALAHAD: answer:first", screen._chat_log)
                    self.assertNotIn("second", screen._chat_log)

                    # Once the first request is fully delivered, a new turn
                    # gets its own timer and result normally.
                    self.assertTrue(screen._submit("third"))
                    for _ in range(80):
                        await asyncio.sleep(0.025)
                        await pilot.pause()
                        if screen._active_request is None:
                            break
                    self.assertEqual(ai.calls, ["first", "third"])
                    self.assertNotIn("trace:third", screen._chat_log)
                    self.assertIn("GALAHAD: answer:third", screen._chat_log)

    def test_chat_input_history_and_session_search(self) -> None:
        asyncio.run(self._chat_history_session_scenario())

    async def _chat_history_session_scenario(self) -> None:
        from tui.deepseek import ChatResponse
        from tui.screens import AskSessionScreen, ChatInput, ChatScreen

        class _FakeAI:
            available = True

            def chat(self, query, context=None):
                return ChatResponse(content=f"echo:{query}")

        with tempfile.TemporaryDirectory() as tmp:
            state = os.path.join(tmp, "state")
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w") as handle:
                json.dump({"state_root": state}, handle)
            with mock.patch.dict(os.environ, {"STAMMTISCH_CONFIG": cfg_path}):
                app = StammtischTUI(binary="/nonexistent/stammtisch-core", skip_boot=True)
                async with app.run_test(size=(100, 30)) as pilot:
                    app.push_screen(ChatScreen(_FakeAI()))
                    await pilot.pause()
                    screen = app.screen
                    self.assertTrue(screen.query_one("#chat-scroll").styles.overflow_y == "scroll"
                                    or str(screen.query_one("#chat-scroll").styles.overflow_y) == "scroll")
                    box = screen.query_one("#chat-input", ChatInput)
                    self.assertTrue(screen._submit("first pick"))
                    for _ in range(40):
                        await asyncio.sleep(0.025)
                        await pilot.pause()
                        if screen._active_request is None:
                            break
                    self.assertTrue(screen._submit("second pick"))
                    for _ in range(40):
                        await asyncio.sleep(0.025)
                        await pilot.pause()
                        if screen._active_request is None:
                            break
                    await pilot.press("up")
                    await pilot.pause()
                    self.assertEqual(box.text, "second pick")
                    await pilot.press("up")
                    await pilot.pause()
                    self.assertEqual(box.text, "first pick")
                    session_id = screen._session["id"]
                    app.pop_screen()
                    await pilot.pause()
                    app.push_screen(ChatScreen(_FakeAI(), session_id=session_id))
                    await pilot.pause()
                    self.assertIn("first pick", app.screen._chat_log)
                    self.assertIn("second pick", app.screen._chat_log)
                    app.screen.action_open_sessions()
                    await pilot.pause()
                    self.assertIsInstance(app.screen, AskSessionScreen)
                    query = app.screen.query_one("#ask-query")
                    query.value = "second"
                    app.screen._reload("second")
                    await pilot.pause()
                    ids = [opt.id for opt in app.screen.query_one("#ask-sessions").options]
                    self.assertIn(session_id, ids)


if __name__ == "__main__":
    unittest.main()
