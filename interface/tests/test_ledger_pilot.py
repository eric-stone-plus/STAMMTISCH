"""Ledger Pilot tests: the absorbed LEDGER screen mounts through the
router against a tmp state root, renders FIFO positions + fills, keeps
marks honest when the quote leg serves nothing, refreshes with r, and
pops with Esc. The quote leg is monkeypatched — no live feed."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from interface.app.shell import WorkstationShell
from interface.collectors.demo import demo_snapshot
from interface.screens.ledger import LedgerScreen
from interface.snapshot import WorkstationSnapshot


def _harness(root=None):
    state = {"t": 1_000.0}

    def provider() -> WorkstationSnapshot:
        state["t"] += 0.5
        return demo_snapshot(state["t"])

    def clock() -> float:
        return state["t"] + 0.25

    return provider, clock


def _write_ledger(root, fills):
    path = root / "intel" / "portfolio" / "ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "fills": fills}),
                    encoding="utf-8")
    return path


def _fill(ts, side, symbol, qty, price, fill_id, broker="paper"):
    return {"id": fill_id, "ts": ts, "broker": broker, "symbol": symbol,
            "side": side, "qty": qty, "price": price, "fee": 0.0}


FILLS = [
    _fill("2026-01-01T09:00:00", "buy", "AAPL", 100, 100.0, "f1"),
    _fill("2026-01-02T09:00:00", "buy", "AAPL", 100, 120.0, "f2"),
    _fill("2026-01-03T09:00:00", "sell", "AAPL", 60, 150.0, "f3"),
    _fill("2026-01-03T10:00:00", "buy", "600519.SS", 10, 1_500.0, "f4"),
]


async def _await_frame(screen: LedgerScreen, count: int = 1,
                       timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while screen.frames_landed < count:
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail(f"no ledger frame landed within {timeout_s}s")
        await asyncio.sleep(0.02)


def test_ledger_screen_renders_positions_and_fills(tmp_path,
                                                   monkeypatch) -> None:
    _write_ledger(tmp_path, FILLS)
    served = {"AAPL": {"last": 130.0, "source": "stub"}}
    monkeypatch.setattr(
        "interface.services.feeds.fetch_batch", lambda symbols: served)

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, root=tmp_path,
                               interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.run_command("ledger")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, LedgerScreen)
            await _await_frame(screen)
            frame = screen.last_frame
            assert frame is not None and frame.ok
            aapl = next(r for r in frame.positions if r.symbol == "AAPL")
            assert aapl.net_qty == pytest.approx(140.0)
            assert aapl.realized_pnl == pytest.approx(3_000.0)
            assert aapl.last == pytest.approx(130.0), "the mark served"
            assert aapl.unrealized == pytest.approx(
                (130.0 - aapl.avg_cost) * 140.0)
            assert screen.query_one("#lg-positions").row_count == 2
            assert screen.query_one("#lg-fills").row_count == 4
            status = screen.query_one("#lg-status")
            assert "2 position(s) · 4 fill(s)" in str(status.render())
            # r re-reads; j/k move the focused table; esc pops.
            landed = screen.frames_landed
            await pilot.press("r")
            await _await_frame(screen, landed + 1)
            positions = screen.query_one("#lg-positions")
            positions.focus()
            await pilot.press("j")
            assert positions.cursor_row == 1
            await pilot.press("escape")
            await pilot.pause()
            assert screen not in app.screen_stack

    asyncio.run(scenario())


def test_missing_marks_render_honestly(tmp_path, monkeypatch) -> None:
    _write_ledger(tmp_path, FILLS[:1])
    monkeypatch.setattr(
        "interface.services.feeds.fetch_batch", lambda symbols: {})

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, root=tmp_path,
                               interval_s=0.05, clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.goto("ledger")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, LedgerScreen)
            await _await_frame(screen)
            (row,) = screen.last_frame.positions
            assert row.last is None and row.unrealized is None
            cells = " ".join(str(cell) for cell in
                            screen.query_one("#lg-positions").get_row_at(0))
            assert "—" in cells, "a missing mark is an em dash, never a price"

    asyncio.run(scenario())


def test_no_state_root_says_so_honestly(monkeypatch) -> None:
    monkeypatch.setattr(
        "interface.services.feeds.fetch_batch", lambda symbols: {})

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.goto("ledger")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, LedgerScreen)
            await _await_frame(screen)
            frame = screen.last_frame
            assert frame.ok and frame.positions == () and frame.fills == ()
            status = screen.query_one("#lg-status")
            assert "(no state root)" in str(status.render())

    asyncio.run(scenario())
