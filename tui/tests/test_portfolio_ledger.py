"""Trade ledger — FIFO accounting, persistence, screen render (offline)."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from services import portfolio
from tui.screens.ledger import LedgerScreen


class FifoTest(unittest.TestCase):
    def test_long_lots_partial_close(self):
        fills = [
            {"ts": "2026-01-01", "id": "a", "broker": "alpaca", "symbol": "AAPL",
             "side": "buy", "qty": 1, "price": 100},
            {"ts": "2026-01-02", "id": "b", "broker": "alpaca", "symbol": "AAPL",
             "side": "buy", "qty": 1, "price": 110},
            {"ts": "2026-01-03", "id": "c", "broker": "alpaca", "symbol": "AAPL",
             "side": "sell", "qty": 1.5, "price": 120},
        ]
        rows = portfolio.positions(fills)
        row = rows[0]
        self.assertEqual(row["net_qty"], 0.5)
        self.assertAlmostEqual(row["avg_cost"], 110.0)
        self.assertAlmostEqual(row["realized_pnl"], 25.0)  # (20*1) + (10*0.5)

    def test_short_open_then_cover(self):
        fills = [
            {"ts": "2026-01-01", "id": "a", "broker": "binance",
             "symbol": "BTCUSDT", "side": "sell", "qty": 1, "price": 100},
            {"ts": "2026-01-02", "id": "b", "broker": "binance",
             "symbol": "BTCUSDT", "side": "buy", "qty": 0.5, "price": 90},
        ]
        rows = portfolio.positions(fills)
        row = rows[0]
        self.assertEqual(row["net_qty"], -0.5)
        self.assertAlmostEqual(row["avg_cost"], 100.0)
        self.assertAlmostEqual(row["realized_pnl"], 5.0)  # (100-90)*0.5

    def test_symbols_and_brokers_are_separate_books(self):
        fills = [
            {"ts": "2026-01-01", "id": "a", "broker": "alpaca", "symbol": "AAPL",
             "side": "buy", "qty": 1, "price": 100},
            {"ts": "2026-01-01", "id": "b", "broker": "binance", "symbol": "AAPL",
             "side": "sell", "qty": 1, "price": 90},
        ]
        rows = portfolio.positions(fills)
        self.assertEqual(len(rows), 2)

    def test_invalid_fills_are_skipped_not_fatal(self):
        fills = [
            {"ts": "2026-01-01", "id": "a", "broker": "alpaca", "symbol": "AAPL",
             "side": "buy", "qty": 1, "price": 100},
            {"ts": "2026-01-02", "id": "b", "broker": "alpaca", "symbol": "AAPL",
             "side": "buy", "qty": "bad", "price": 100},
        ]
        rows = portfolio.positions(fills)
        self.assertEqual(rows[0]["net_qty"], 1)


class PersistenceTest(unittest.TestCase):
    def test_add_roundtrip_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            fill = portfolio.add_fill(tmp, "buy", " aapl ", "1", "332.0",
                                      broker="alpaca")
            self.assertEqual(fill["symbol"], "AAPL")
            self.assertEqual(portfolio.load(tmp), [fill])
            self.assertTrue(portfolio.remove_fill(tmp, fill["id"]))
            self.assertEqual(portfolio.load(tmp), [])
            self.assertFalse(portfolio.remove_fill(tmp, fill["id"]))

    def test_validation_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            for kwargs in (
                {"side": "yolo", "symbol": "AAPL", "qty": 1, "price": 1},
                {"side": "buy", "symbol": "", "qty": 1, "price": 1},
                {"side": "buy", "symbol": "AAPL", "qty": 0, "price": 1},
                {"side": "buy", "symbol": "AAPL", "qty": 1, "price": -1},
                {"side": "buy", "symbol": "AAPL", "qty": 1, "price": 1,
                 "fee": -1},
            ):
                with self.assertRaises(portfolio.LedgerError):
                    portfolio.add_fill(tmp, **kwargs)

    def test_corrupt_ledger_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = portfolio.ledger_path(tmp)
            path.parent.mkdir(parents=True)
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(portfolio.LedgerError):
                portfolio.load(tmp)


class LedgerScreenSmokeTest(unittest.TestCase):
    def test_positions_render_from_seeded_ledger(self):
        asyncio.run(self._scenario())

    async def _scenario(self) -> None:
        from textual.app import App
        from textual.widgets import DataTable, Static

        with tempfile.TemporaryDirectory() as tmp:
            portfolio.add_fill(tmp, "buy", "AAPL", "2", "300.0", broker="alpaca")
            screen = LedgerScreen(
                SimpleNamespace(state_root=tmp), SimpleNamespace())
            with mock.patch("services.livefeed.fetch_batch",
                            return_value={"AAPL": {
                                "last": 320.0, "prev_close": 310.0,
                                "name": "Apple", "source": "test"}}):
                class Host(App):
                    pass

                async with Host().run_test() as pilot:
                    host = pilot.app
                    host.push_screen(screen)
                    await pilot.pause()
                    await pilot.pause()
                    table = screen.query_one("#ledger-positions", DataTable)
                    self.assertEqual(table.row_count, 1)
                    row = table.get_row_at(0)
                    self.assertEqual(str(row[1]), "AAPL")
                    self.assertEqual(str(row[2]), "+2")
                    self.assertEqual(str(row[5]), "+40.00")  # (320-300)*2
                    status = screen.query_one("#ledger-status", Static)
                    self.assertIn("1 position(s)", str(status.render()))


if __name__ == "__main__":
    unittest.main()
