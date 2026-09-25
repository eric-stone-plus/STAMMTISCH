"""The :delete write path under Pilot: fail-closed dialog, audited both
ways, single-flight off-thread execution against the FAKE core binary.

These pin the M5 doctrine end to end:

- unknown run id -> notify, NO dialog (validation precedes everything);
- the dialog never action by accident: Esc, 30s silence, and Enter on
  the default-focused Cancel all resolve CANCEL and are audited;
- an explicit Confirm runs the core delete OFF the UI thread, lands as
  notify + activity-feed audit lines (``ui.delete confirmed`` plus the
  core's own verdict with ``data.removed``), and forces one spine
  refresh;
- delete failures surface as notify + audit, never a crash;
- a second delete while one is in flight is refused (single flight).
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from interface.app.confirm import ConfirmDialog
from interface.app.shell import WorkstationShell
from interface.collectors.core_cli import CoreCliClient
from interface.collectors.demo import demo_snapshot
from interface.screens.overview import OverviewScreen
from interface.snapshot import WorkstationSnapshot

FIXTURES = Path(__file__).parent / "fixtures"


def _harness(base: float = 1_000.0, step: float = 0.5, delete_run=None):
    state = {"t": base}

    def provider() -> WorkstationSnapshot:
        state["t"] += step
        return demo_snapshot(state["t"])

    def clock() -> float:
        return state["t"] + step / 2

    app = WorkstationShell(provider=provider, interval_s=0.05, clock=clock,
                           delete_run=delete_run)
    return app


def _fake_core(tmp_path: Path) -> Path:
    binary = tmp_path / "fake-stammtisch-core"
    binary.write_text((FIXTURES / "fake_core.py").read_text(encoding="utf-8"),
                      encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _messages(app) -> list[str]:
    return [n.message for n in app._notifications]


def _audit_lines(app) -> list[str]:
    """The audit chrome lines the write path appended to the feed."""
    base = app.screen_stack[0]
    feed = base.query_one("#feed")
    return [strip.text for strip in feed.lines if "[audit]" in strip.text]


async def _until(predicate, timeout: float = 10.0) -> bool:
    waited = 0.0
    while waited < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.02)
        waited += 0.02
    return predicate()


def _run(scenario) -> None:
    asyncio.run(scenario())


# ── gate: unknown id never opens the dialog ───────────────────────────


def test_unknown_id_from_the_bar_never_opens_the_dialog() -> None:
    async def scenario() -> None:
        app = _harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            await pilot.press(":")
            await pilot.press(*"delete no-such-run")
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, OverviewScreen)
            assert not any(isinstance(s, ConfirmDialog)
                           for s in app.screen_stack)
            assert any("no such run" in m for m in _messages(app))
            assert _audit_lines(app) == [], "nothing audited, nothing done"

    _run(scenario)


# ── fail-closed resolution: esc / silence / default-cancel enter ──────


def test_enter_alone_resolves_cancel_cancel_is_the_default() -> None:
    async def scenario() -> None:
        app = _harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            app.router.run_command("delete demo-run-live")
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, ConfirmDialog)
            assert dialog.focus_history[0] == "#confirm-cancel", (
                "Cancel holds focus by default")
            await pilot.press("enter")  # activates the FOCUSED Cancel
            await pilot.pause()
            assert dialog.last_resolution == ("button", False)
            assert isinstance(app.screen, OverviewScreen), "dialog dismissed"
            assert any("ui.delete cancelled run=demo-run-live" in line
                       for line in _audit_lines(app))
            assert any("delete cancelled" in m for m in _messages(app))

    _run(scenario)


def test_esc_resolves_cancel_and_is_audited() -> None:
    async def scenario() -> None:
        app = _harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            app.router.run_command("delete demo-run-live")
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, ConfirmDialog)
            await pilot.press("escape")
            await pilot.pause()
            assert dialog.last_resolution == ("esc", False)
            assert isinstance(app.screen, OverviewScreen)
            assert any("ui.delete cancelled run=demo-run-live" in line
                       for line in _audit_lines(app))

    _run(scenario)


def test_silence_timeout_resolves_cancel_and_is_audited() -> None:
    async def scenario() -> None:
        app = _harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            # Wide margins: pilot.pause() costs real wall time here
            # (observed 0.6–1.2s: wait_for_idle against the shell's 20Hz
            # refresh + full-size render), so the silence window must
            # exceed the worst-case pause or the timer legitimately
            # expires before the assert. Semantics pinned: "resolves on
            # its own without input", not milliseconds.
            dialog = ConfirmDialog("demo-run-live", app._delete_resolved,
                                   silence_s=5.0)
            app.push_screen(dialog)
            await pilot.pause()
            assert app.screen is dialog
            assert await _until(lambda: dialog._resolved, timeout=10.0), (
                "30s-of-silence (shortened) resolves on its own")
            await pilot.pause()
            assert dialog.last_resolution == ("silence", False)
            assert isinstance(app.screen, OverviewScreen)
            assert any("ui.delete cancelled run=demo-run-live" in line
                       for line in _audit_lines(app))

    _run(scenario)


def test_any_key_rearms_the_silence_window() -> None:
    async def scenario() -> None:
        app = _harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            # Adaptive margins: pilot.pause()/press() each consume real
            # wall time here (observed ~1s: wait_for_idle against the
            # shell's 20Hz refresh), so fixed sleeps race the dialog's
            # deadlines. Semantics pinned: the assert lands AFTER the
            # original deadline (t_push + silence_s) yet BEFORE the
            # re-armed one (~t_press + silence_s) — sleep to the
            # midpoint of that window, whatever wall time the presses
            # consumed.
            silence_s = 6.0
            t_push = time.monotonic()
            dialog = ConfirmDialog("demo-run-live", app._delete_resolved,
                                   silence_s=silence_s)
            app.push_screen(dialog)
            await pilot.pause()
            await asyncio.sleep(1.0)
            await pilot.press("right")  # activity: re-arm the window
            t_press = time.monotonic()
            elapsed = t_press - t_push
            assert elapsed < silence_s, (
                "precondition: the re-arming press must land inside the "
                f"original window (took {elapsed:.1f}s of wall time)")
            await asyncio.sleep(silence_s - elapsed / 2.0)
            assert not dialog._resolved, (
                "past the original deadline but alive: only the re-armed "
                "window is running")
            await pilot.press("escape")
            await pilot.pause()
            assert dialog.last_resolution == ("esc", False)

    _run(scenario)


def test_help_never_stacks_over_confirm_and_silence_still_cancels() -> None:
    """The overlay-over-confirm contract, pinned (round F, S-1): the
    sheet REFUSES to stack over the ONE write path's dialog — Textual's
    ``Screen.dismiss()`` pops the TOPMOST screen, so a mid-stack silence
    resolution under a cover would pop the wrong screen and strand the
    spent dialog (inert, unclosable). The refusal is a notify; the
    dialog stays on top and its fail-closed silence still resolves
    CANCEL — audited, never spawning the delete. Help never actions."""
    calls: list[str] = []

    async def scenario() -> None:
        app = _harness(delete_run=calls.append)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            # Wide margin: the ? press must land BEFORE the silence
            # fires even under full-suite load (pilot.pause() costs
            # real wall time here — keep the window ≫ the worst pause).
            dialog = ConfirmDialog("demo-run-live", app._delete_resolved,
                                   silence_s=5.0)
            app.push_screen(dialog)
            await pilot.pause()
            assert app.screen is dialog
            await pilot.press("question_mark")
            await pilot.pause()
            assert app.screen is dialog, (
                "the sheet must never stack over the confirm dialog")
            assert any("confirm dialog" in m for m in _messages(app)), (
                "the refusal is announced")
            assert await _until(lambda: dialog._resolved, timeout=10.0), (
                "the fail-closed silence resolves on its own")
            await pilot.pause()
            assert dialog.last_resolution == ("silence", False)
            assert calls == [], "silence never actions: no delete spawned"
            assert any("ui.delete cancelled run=demo-run-live" in line
                       for line in _audit_lines(app))
            # Topmost dismiss: the spent dialog leaves the stack cleanly.
            assert dialog not in app.screen_stack
            assert isinstance(app.screen, OverviewScreen)

    _run(scenario)


def test_left_right_cycle_the_focus() -> None:
    async def scenario() -> None:
        app = _harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            app.router.run_command("delete demo-run-live")
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, ConfirmDialog)
            await pilot.press("right")
            assert dialog.focus_history[-1] == "#confirm-confirm"
            await pilot.press("right")  # wraps back to Cancel
            assert dialog.focus_history[-1] == "#confirm-cancel"
            await pilot.press("left")
            assert dialog.focus_history[-1] == "#confirm-confirm", (
                "left from Cancel wraps to Confirm")
            await pilot.press("escape")
            await pilot.pause()
            assert dialog.last_resolution[1] is False

    _run(scenario)


# ── the confirmed path: fake core, off-thread, audited, forced refresh ─


def test_confirm_deletes_via_the_fake_core_and_audits_the_verdict(
        tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "spawns.log"
    monkeypatch.setenv("FAKE_CORE_LOG", str(log))
    fake = _fake_core(tmp_path)
    client = CoreCliClient(root=tmp_path, binary=str(fake))

    async def scenario() -> None:
        app = _harness(delete_run=client.delete)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            counts_before = dict(app.spine.update_counts)
            app.router.run_command("delete demo-run-live")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmDialog)
            await pilot.press("right")  # focus Confirm
            await pilot.press("enter")  # explicit confirm
            await pilot.pause()
            assert await _until(lambda: any(
                "core.delete ok" in line for line in _audit_lines(app)))
            # The fake core received exactly one delete spawn, right argv.
            lines = log.read_text(encoding="utf-8").splitlines()
            assert any(line.endswith("delete demo-run-live --json")
                       for line in lines)
            # The full audit trail, in order.
            audit = _audit_lines(app)
            confirmed_at = next(i for i, line in enumerate(audit)
                                if "ui.delete confirmed run=demo-run-live"
                                in line)
            verdict_at = next(i for i, line in enumerate(audit)
                              if "core.delete ok run=demo-run-live "
                              "removed=True" in line)
            assert verdict_at > confirmed_at
            assert any("deleted run demo-run-live" in m
                       for m in _messages(app))
            # One spine refresh was forced: every panel got one more poll.
            assert await _until(lambda: all(
                app.spine.update_counts[p] >= counts_before[p] + 1
                for p in counts_before))
            assert not app._delete_flight.in_flight, "slot freed on landing"
            assert client.spawn_count == 1, "exactly one core spawn"

    _run(scenario)


def test_delete_error_surfaces_as_notify_and_audit_not_a_crash() -> None:
    def broken_delete(run_id: str):
        del run_id
        raise RuntimeError("core exploded")

    async def scenario() -> None:
        app = _harness(delete_run=broken_delete)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            app.router.run_command("delete demo-run-live")
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()
            assert await _until(lambda: any(
                "core.delete error run=demo-run-live" in line
                for line in _audit_lines(app)))
            assert any("delete failed" in m for m in _messages(app))
            assert isinstance(app.screen, OverviewScreen), "no crash"
            assert not app._delete_flight.in_flight

    _run(scenario)


def test_core_error_envelope_is_audited_as_a_failed_delete(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CORE_DELETE_FAIL", "demo-run-live")
    fake = _fake_core(tmp_path)
    client = CoreCliClient(root=tmp_path, binary=str(fake))

    async def scenario() -> None:
        app = _harness(delete_run=client.delete)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            app.router.run_command("delete demo-run-live")
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()
            assert await _until(lambda: any(
                "core.delete failed run=demo-run-live" in line
                for line in _audit_lines(app)))
            assert not any("core.delete ok" in line
                           for line in _audit_lines(app))
            assert any("delete failed" in m for m in _messages(app))
            assert isinstance(app.screen, OverviewScreen)

    _run(scenario)


def test_confirm_without_a_write_lane_refuses_honestly() -> None:
    async def scenario() -> None:
        app = _harness(delete_run=None)  # demo: no core write lane
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            app.router.run_command("delete demo-run-live")
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()
            assert any("ui.delete confirmed run=demo-run-live" in line
                       for line in _audit_lines(app))
            assert any("core.delete refused run=demo-run-live" in line
                       for line in _audit_lines(app))
            assert any("no core write lane" in m for m in _messages(app))
            assert isinstance(app.screen, OverviewScreen)

    _run(scenario)


def test_second_delete_while_one_is_in_flight_is_refused() -> None:
    release = threading.Event()
    started = threading.Event()

    def slow_delete(run_id: str):
        started.set()
        release.wait(10.0)
        from interface.collectors.core_cli import CliEnvelope

        return CliEnvelope(True, "delete", data={"removed": True})

    async def scenario() -> None:
        app = _harness(delete_run=slow_delete)
        async with app.run_test(size=(120, 40)) as pilot:
            await asyncio.sleep(0.3)
            await pilot.pause()
            # First delete: confirmed, worker blocks inside the core call.
            app.router.run_command("delete demo-run-live")
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()
            assert await _until(started.is_set)
            assert app._delete_flight.in_flight
            # Second delete while the first is still running: refused.
            app.router.run_command("delete demo-run-final")
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("enter")
            await pilot.pause()
            assert any("already in flight" in m for m in _messages(app))
            release.set()
            assert await _until(lambda: any(
                "core.delete ok run=demo-run-live" in line
                for line in _audit_lines(app)))
            assert not app._delete_flight.in_flight

    _run(scenario)
