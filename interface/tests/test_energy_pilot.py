"""Energy Pilot tests: the absorbed ENERGY screen mounts through the
router with a stubbed service (nothing talks to the net), renders the
watchlist rows (landed + degraded), updates the detail pane on cursor
moves, and renders the fail-closed no-config error honestly."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from interface.app.shell import WorkstationShell
from interface.collectors.demo import demo_snapshot
from interface.screens.energy import EnergyScreen
from interface.services.energy import EnergyFrame, SeriesRow
from interface.snapshot import WorkstationSnapshot


def _harness():
    state = {"t": 1_000.0}

    def provider() -> WorkstationSnapshot:
        state["t"] += 0.5
        return demo_snapshot(state["t"])

    def clock() -> float:
        return state["t"] + 0.25

    return provider, clock


def _row(key, label, value, **kw) -> SeriesRow:
    base = {"key": key, "group": "CRUDE", "label": label, "unit": "$/bbl",
            "decimals": 2, "frequency": "daily", "period": "2026-09-21",
            "value": value, "change": kw.pop("change", 1.0), "route": "r",
            "history": (("2026-09-21", value), ("2026-09-20", value - 1.0)),
            "description": "desc"}
    base.update(kw)
    return SeriesRow(**base)


STUB_FRAME = EnergyFrame(
    ok=True,
    rows=(
        _row("wti_spot", "WTI Cushing spot", 71.5),
        _row("brent_spot", "Brent spot", 75.25, change=-0.5),
        SeriesRow(key="henry_hub_spot", group="GAS",
                  label="Henry Hub spot", unit="$/MMBtu", decimals=2,
                  frequency="daily", route="r",
                  error="configured proxy is unavailable"),
    ),
    via="configured proxy",
)


async def _await_frame(screen: EnergyScreen, count: int = 1,
                       timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while screen.frames_landed < count:
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail(f"no energy frame landed within {timeout_s}s")
        await asyncio.sleep(0.02)


def test_energy_screen_renders_the_watchlist_and_detail(monkeypatch) -> None:
    monkeypatch.setattr("interface.services.energy.fetch_watchlist",
                        lambda *a, **kw: STUB_FRAME)

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.run_command("energy")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, EnergyScreen)
            await _await_frame(screen)
            table = screen.query_one("#eg-table")
            assert table.row_count == 3, "landed + error rows all keep seats"
            assert "2/3 series" in str(
                screen.query_one("#eg-status").render())
            row = " ".join(str(c) for c in table.get_row_at(2))
            assert "ERR" in row, "the failed series is marked, not hidden"
            # j moves the cursor and the detail pane follows.
            table.focus()
            await pilot.press("j")
            await pilot.pause()
            detail = str(screen.query_one("#eg-detail").render())
            assert "Brent spot" in detail
            assert "EIA Open Data API v2 · read-only" in detail, (
                "the API attribution rides along")
            await pilot.press("escape")
            await pilot.pause()
            assert screen not in app.screen_stack

    asyncio.run(scenario())


def test_energy_screen_fail_closed_without_config(monkeypatch) -> None:
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    monkeypatch.delenv("STAMMTISCH_ENERGY_PROXY", raising=False)

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.goto("energy")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, EnergyScreen)
            await _await_frame(screen)
            status = str(screen.query_one("#eg-status").render())
            assert "no EIA API key configured" in status
            assert screen.query_one("#eg-table").row_count == 0, (
                "fail-closed: no fabricated rows")

    asyncio.run(scenario())
