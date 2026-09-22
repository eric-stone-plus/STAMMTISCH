"""Polymarket Pilot tests: the absorbed POLYMARKET screen mounts through
the router with a stubbed service (nothing talks to the net), renders
the tape, the LOCAL ``/`` filter narrows it screen-side, the detail
pane follows the cursor, and the fail-closed no-proxy error renders."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from interface.app.shell import WorkstationShell
from interface.collectors.demo import demo_snapshot
from interface.screens.polymarket import PolymarketScreen
from interface.services.polymarket import MarketRow, MarketTape
from interface.snapshot import WorkstationSnapshot


def _harness():
    state = {"t": 1_000.0}

    def provider() -> WorkstationSnapshot:
        state["t"] += 0.5
        return demo_snapshot(state["t"])

    def clock() -> float:
        return state["t"] + 0.25

    return provider, clock


def _row(id_, question, slug, yes) -> MarketRow:
    return MarketRow(id=id_, question=question, slug=slug, yes=yes,
                     volume24hr=1_234_567.0, volume=None, end="2026-12-31",
                     fee_rate=0.02, category="Politics")


STUB_TAPE = MarketTape(
    ok=True,
    markets=(
        _row("7", "Will the bill pass by March?", "bill-march", 0.65),
        _row("8", "Will the rocket reach orbit?", "rocket-orbit", 0.42),
        _row("9", "Will the summit end early?", "summit-early", 0.10),
    ),
    via="configured proxy",
)


async def _await_frame(screen: PolymarketScreen, count: int = 1,
                       timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while screen.frames_landed < count:
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail(f"no tape landed within {timeout_s}s")
        await asyncio.sleep(0.02)


def test_polymarket_screen_filters_locally_and_shows_detail(
        monkeypatch) -> None:
    monkeypatch.setattr("interface.services.polymarket.fetch_markets",
                        lambda *a, **kw: STUB_TAPE)

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.run_command("polymarket")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, PolymarketScreen)
            await _await_frame(screen)
            table = screen.query_one("#pm-table")
            assert table.row_count == 3
            assert "3 active markets" in str(
                screen.query_one("#pm-status").render())
            # The / filter is screen-side view state over the served tape.
            await pilot.press("slash")
            await pilot.pause()
            assert isinstance(screen.focused, type(
                screen.query_one("#pm-filter")))
            await pilot.press(*"rocket")
            await pilot.pause()
            assert table.row_count == 1, "the local filter narrows the tape"
            assert '1/3 markets match "rocket"' in str(
                screen.query_one("#pm-status").render())
            # Enter hands focus to the table; j moves and detail follows.
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("j")
            await pilot.pause()
            detail = str(screen.query_one("#pm-detail").render())
            assert "Will the rocket reach orbit?" in detail
            assert "YES 42.0%" in detail
            assert "Read-only market data · no order path" in detail
            # Clearing the filter restores the full tape.
            await pilot.press("slash")
            await pilot.press(*"        ")  # 7 spaces clear 6 chars
            await pilot.pause()
            assert table.row_count == 3
            await pilot.press("escape")
            await pilot.pause()
            assert screen not in app.screen_stack

    asyncio.run(scenario())


def test_polymarket_screen_fail_closed_without_a_proxy(monkeypatch) -> None:
    monkeypatch.delenv("STAMMTISCH_POLYMARKET_PROXY", raising=False)

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.goto("polymarket")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, PolymarketScreen)
            await _await_frame(screen)
            status = str(screen.query_one("#pm-status").render())
            assert "no Polymarket proxy configured" in status
            assert screen.query_one("#pm-table").row_count == 0, (
                "fail-closed: no fabricated markets")

    asyncio.run(scenario())
