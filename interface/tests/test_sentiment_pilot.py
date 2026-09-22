"""Sentiment Pilot tests: the absorbed SENTIMENT screen mounts through
the router against a tmp reports root (env-pointed — no real workspace
path is read), renders the latest tape, walks the day history with
←/→, hits the oldest/newest edges, and notifies the M7 deferral on
[O]."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from interface.app.shell import WorkstationShell
from interface.collectors.demo import demo_snapshot
from interface.screens.sentiment import SentimentScreen
from interface.snapshot import WorkstationSnapshot


def _harness():
    state = {"t": 1_000.0}

    def provider() -> WorkstationSnapshot:
        state["t"] += 0.5
        return demo_snapshot(state["t"])

    def clock() -> float:
        return state["t"] + 0.25

    return provider, clock


def _report(root, day, headlines):
    out = root / day / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"fin-daily-{day}.refined.json").write_text(json.dumps({
        "date": day, "model": "test-model",
        "brief": [{"title": h} for h in headlines],
        "markets": {}, "notes": [],
    }), encoding="utf-8")


def _seed_days(tmp_path):
    root = tmp_path
    _report(root, "20260101", ["Fed holds rates steady"])
    _report(root, "20260102", ["Earnings beat drives gains",
                               "Revenue miss as profits drop"])
    _report(root, "20260103", ["恒指 放量 上涨"])
    return root


async def _await_doc(screen: SentimentScreen, count: int = 1,
                     timeout_s: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while screen.docs_landed < count:
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail(f"no report landed within {timeout_s}s")
        await asyncio.sleep(0.02)


def test_sentiment_screen_renders_and_walks_the_history(
        tmp_path, monkeypatch) -> None:
    root = _seed_days(tmp_path)
    monkeypatch.delenv("GALAHAD_REPORTS_ROOT", raising=False)
    monkeypatch.setenv("STAMMTISCH_REPORTS", str(root))

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.run_command("sentiment")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, SentimentScreen)
            await _await_doc(screen)
            assert screen.doc is not None
            assert screen.doc["date"] == "20260103", "newest first"
            header = str(screen.query_one("#sent-header").render())
            assert "20260103" in header and "day 3/3" in header
            body = str(screen.query_one("#sent-body").render())
            assert "MARKET SENTIMENT" in body
            assert "test-model" in header, "provenance survives"
            # ← walks back through the indexed days.
            await pilot.press("left")
            await _await_doc(screen, 2)
            assert screen.doc["date"] == "20260102"
            await pilot.press("left")
            await _await_doc(screen, 3)
            assert screen.doc["date"] == "20260101"
            await pilot.press("left")
            await pilot.pause()
            assert any("Oldest report." in n.message
                       for n in app._notifications)
            # → walks forward again.
            await pilot.press("right")
            await _await_doc(screen, 4)
            assert screen.doc["date"] == "20260102"
            # [O] is the deferred GALAHAD handoff: notify, no action.
            await pilot.press("o")
            await pilot.pause()
            assert any("audited chat lane" in n.message
                       for n in app._notifications)
            await pilot.press("escape")
            await pilot.pause()
            assert screen not in app.screen_stack

    asyncio.run(scenario())


def test_sentiment_screen_is_honest_with_no_history(tmp_path,
                                                    monkeypatch) -> None:
    empty = tmp_path / "empty-reports"
    empty.mkdir()
    monkeypatch.delenv("GALAHAD_REPORTS_ROOT", raising=False)
    monkeypatch.setenv("STAMMTISCH_REPORTS", str(empty))

    async def scenario() -> None:
        provider, clock = _harness()
        app = WorkstationShell(provider=provider, interval_s=0.05,
                               clock=clock)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            assert app.router.goto("sentiment")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, SentimentScreen)
            await asyncio.sleep(0.2)
            body = str(screen.query_one("#sent-body").render())
            assert "no indexed report history" in body

    asyncio.run(scenario())
