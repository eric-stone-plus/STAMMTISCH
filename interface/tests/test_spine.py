"""Spine tests: cadence math, hydration latch, single flight, staleness,
bell episodes — the pure core, driven headlessly (no live Textual app).

The RefreshSpine wiring is exercised through a fake app object that
provides exactly the four surface methods the spine touches
(``set_interval`` / ``run_worker`` / ``call_from_thread`` / ``bell``).
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace

from interface.app.spine import (
    PANEL_MULTIPLIERS,
    Cadence,
    ErrorBell,
    RefreshSpine,
    SingleFlight,
    SlowLane,
    staleness_for,
)
from interface.collectors.demo import demo_snapshot
from interface.snapshot import WorkstationSnapshot


class _FakeApp:
    """The four app surfaces the spine needs; workers are real threads."""

    def __init__(self) -> None:
        self.bells = 0
        self.intervals: list[float] = []

    def bell(self) -> None:
        self.bells += 1

    def set_interval(self, interval: float, callback: object) -> object:
        self.intervals.append(interval)
        return SimpleNamespace(stop=lambda: None)

    def run_worker(self, work: Callable[[], None], **kwargs: object) -> None:
        threading.Thread(target=work, daemon=True).start()

    def call_from_thread(self, callback: Callable[..., None],
                         *args: object) -> None:
        callback(*args)


class _Recorder:
    def __init__(self) -> None:
        self.frames: list[tuple[WorkstationSnapshot, frozenset[str]]] = []
        self.stale_updates: list[dict[str, str]] = []

    def spine_frame(self, frame: WorkstationSnapshot,
                    due: frozenset[str]) -> None:
        self.frames.append((frame, due))

    def spine_staleness(self, levels: dict[str, str]) -> None:
        self.stale_updates.append(levels)


def _wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


# -- cadence math -----------------------------------------------------------


def test_cadence_counts_match_multipliers_over_30_ticks() -> None:
    cadence = Cadence()
    assert cadence.observe_frame(demo_snapshot(1_000.0)), "rows hydrate"
    counts: Counter[str] = Counter()
    for _ in range(30):
        counts.update(cadence.tick())
    assert counts["feed"] == 30
    assert counts["banner"] == 30
    assert counts["runs"] == 15, "x2 panel refreshes every 2nd tick"
    assert counts["glance"] == 6, "x5 panel refreshes every 5th tick"
    assert counts["services"] == 2, "x15 panel refreshes every 15th tick"
    assert counts["services"] < 30, "forced-every-frame must be impossible"


def test_cadence_scheduling_is_multiplier_relative() -> None:
    cadence = Cadence({"a": 1, "b": 3})
    cadence.observe_frame(WorkstationSnapshot(taken_at=0.0))
    due = [cadence.tick() for _ in range(3)]
    assert due == [frozenset({"a"}), frozenset({"a"}), frozenset({"a", "b"})]


# -- hydration latch ----------------------------------------------------------


def test_hydration_forces_full_frames_until_rows_arrive() -> None:
    cadence = Cadence()
    everything = frozenset(PANEL_MULTIPLIERS)
    assert cadence.tick() == everything, "boot: every panel every frame"
    warming = WorkstationSnapshot(taken_at=1.0, collector_error="warming up")
    assert not cadence.observe_frame(warming), "error + no rows: stays armed"
    assert cadence.tick() == everything
    assert cadence.observe_frame(demo_snapshot(1_000.0)), "late rows disarm"
    assert cadence.hydrated
    assert cadence.tick() != everything, "cadence takes over after hydration"


def test_hydration_zero_run_frame_disarms_and_never_rearms() -> None:
    cadence = Cadence()
    legit_zero = WorkstationSnapshot(taken_at=1.0)
    assert cadence.observe_frame(legit_zero), "healthy zero-run frame is final"
    later_empty = WorkstationSnapshot(taken_at=2.0, collector_error="x")
    assert not cadence.observe_frame(later_empty), "never re-arm on empty"
    everything = frozenset(PANEL_MULTIPLIERS)
    assert cadence.tick() != everything


# -- staleness arithmetic ------------------------------------------------------


def test_staleness_thresholds_are_strictly_greater() -> None:
    assert staleness_for(0.0, 1) == "fresh"
    assert staleness_for(3.0, 1) == "fresh", "3x period exactly: still fresh"
    assert staleness_for(3.01, 1) == "warn"
    assert staleness_for(10.0, 1) == "warn", "10x period exactly: still warn"
    assert staleness_for(10.01, 1) == "crit"
    assert staleness_for(15.0, 5) == "fresh"
    assert staleness_for(15.01, 5) == "warn"
    assert staleness_for(50.01, 5) == "crit"
    assert staleness_for(30.02, 5, interval_s=2.0) == "warn", "period scales"


def test_spine_exposes_staleness_per_panel_multiplier() -> None:
    app = _FakeApp()
    clock = {"now": 0.0}
    spine = RefreshSpine(app, lambda: demo_snapshot(0.0), clock=lambda: clock["now"])
    recorder = _Recorder()
    spine.subscribe(recorder)
    spine._apply_snapshot(WorkstationSnapshot(taken_at=100.0))
    clock["now"] = 116.0  # age 16s: feed crit, runs/glance warn, services fresh
    spine._tick_staleness()
    levels = spine.stale_levels
    assert levels["feed"] == "crit"  # 16 > 10 * 1
    assert levels["banner"] == "crit"
    assert levels["runs"] == "warn"  # 16 in (6, 20]
    assert levels["glance"] == "warn"  # 16 in (15, 50]
    assert levels["services"] == "fresh"  # 16 <= 45
    assert recorder.stale_updates and recorder.stale_updates[-1] == dict(levels)


# -- bell once per episode ------------------------------------------------------


def test_error_bell_rings_on_rising_edge_only() -> None:
    bell = ErrorBell()
    edges = [bell.observe(value) for value in (False, True, True, False, True)]
    assert edges == [False, True, False, False, True]


def test_spine_bells_once_per_collector_error_episode() -> None:
    app = _FakeApp()
    spine = RefreshSpine(app, lambda: demo_snapshot(1_000.0), clock=lambda: 0.0)
    healthy = demo_snapshot(1_000.0)
    broken = replace(healthy, collector_error="disk gone")
    spine._apply_snapshot(broken)
    assert app.bells == 1, "episode start rings"
    spine._apply_snapshot(broken)
    assert app.bells == 1, "still broken: no second ring"
    spine._apply_snapshot(healthy)
    assert app.bells == 1, "recovery is silent"
    spine._apply_snapshot(broken)
    assert app.bells == 2, "a NEW episode rings again"


# -- single flight --------------------------------------------------------------


def test_single_flight_slot_semantics() -> None:
    flight = SingleFlight()
    assert flight.try_start()
    assert not flight.try_start(), "slot held while in flight"
    flight.finish()
    assert flight.try_start()


def test_single_flight_claim_is_thread_safe() -> None:
    """D-M6 finding 2: ``try_start`` (UI thread) vs ``finish()`` (worker
    threads) was a lock-free check-then-set. A barrier'd hammer of N
    concurrent ``try_start`` calls must elect exactly ONE winner, and a
    ``finish()`` from another thread must re-open exactly one slot."""
    THREADS = 8
    flight = SingleFlight()

    def claim(target: SingleFlight, barrier: threading.Barrier,
              results: list[bool], lock: threading.Lock) -> None:
        barrier.wait(10.0)
        won = target.try_start()
        with lock:
            results.append(won)

    for round_ in range(3):
        barrier = threading.Barrier(THREADS)
        results: list[bool] = []
        lock = threading.Lock()
        threads = [threading.Thread(
            target=claim, args=(flight, barrier, results, lock))
            for _ in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10.0)
        assert sum(results) == 1, (
            f"round {round_}: exactly one concurrent claim may win, "
            f"got {sum(results)}/{THREADS}")
        assert flight.in_flight, "the winner holds the slot"
        finisher = threading.Thread(target=flight.finish)
        finisher.start()
        finisher.join(10.0)
        assert not flight.in_flight, "finish from another thread re-opens"


def test_spine_slow_provider_never_overlaps() -> None:
    gate = {"started": threading.Event(), "release": threading.Event()}
    stats = {"calls": 0, "live": 0, "max_live": 0}
    lock = threading.Lock()

    def slow_provider() -> WorkstationSnapshot:
        with lock:
            stats["calls"] += 1
            stats["live"] += 1
            stats["max_live"] = max(stats["max_live"], stats["live"])
        gate["started"].set()
        gate["release"].wait(10.0)
        with lock:
            stats["live"] -= 1
        return demo_snapshot(1_000.0)

    app = _FakeApp()
    spine = RefreshSpine(app, slow_provider, clock=lambda: 1_000.0)
    spine._on_tick()
    assert _wait_until(lambda: gate["started"].is_set())
    for _ in range(5):
        spine._on_tick()  # ticks while in flight must start nothing
    assert stats["calls"] == 1
    assert spine.in_flight
    gate["release"].set()
    assert _wait_until(lambda: not spine.in_flight), "frame lands, slot frees"
    spine._on_tick()
    assert _wait_until(lambda: stats["calls"] == 2)
    assert stats["max_live"] == 1, "provider never ran concurrently"


# -- spine dispatch: due panels only ----------------------------------------------


def test_spine_updates_only_due_panels() -> None:
    app = _FakeApp()
    recorder = _Recorder()
    spine = RefreshSpine(app, lambda: demo_snapshot(1_000.0), clock=lambda: 1_000.0)
    spine.subscribe(recorder)
    spine._on_tick()  # tick 1: hydration -> everything due
    assert _wait_until(lambda: spine.update_counts["feed"] == 1)
    counts_after_boot = dict(spine.update_counts)
    assert set(counts_after_boot.values()) == {1}, "boot frame updates all panels"
    spine._on_tick()  # tick 2: only feed/banner/runs due
    assert _wait_until(lambda: spine.update_counts["feed"] == 2)
    counts = spine.update_counts
    assert counts["banner"] == 2
    assert counts["runs"] == 2
    assert counts["glance"] == 1, "x5 panel not due on tick 2"
    assert counts["services"] == 1, "x15 panel not due on tick 2"
    assert recorder.frames[0][1] == frozenset(PANEL_MULTIPLIERS)


def test_spine_provider_error_degrades_to_banner_frame() -> None:
    app = _FakeApp()
    recorder = _Recorder()

    def broken_provider() -> WorkstationSnapshot:
        raise RuntimeError("collector exploded")

    spine = RefreshSpine(app, broken_provider, clock=lambda: 500.0)
    spine.subscribe(recorder)
    spine._on_tick()
    assert _wait_until(lambda: bool(recorder.frames))
    frame, _ = recorder.frames[0]
    assert frame.collector_error and "collector exploded" in frame.collector_error
    assert app.bells == 1


def test_dispatch_failure_releases_flight_slot() -> None:
    """Review B spec finding: a raising run_worker used to leave the slot
    claimed forever — 10 further ticks, zero provider calls."""
    app = _FakeApp()
    provider_calls = []

    def provider() -> WorkstationSnapshot:
        provider_calls.append(1)
        return WorkstationSnapshot(taken_at=0.0)

    spine = RefreshSpine(app=app, provider=provider, interval_s=0.0)
    spine.start()
    original = app.run_worker

    def raising_run_worker(work, **kwargs):
        raise RuntimeError("dispatch exploded")

    app.run_worker = raising_run_worker  # type: ignore[method-assign]
    for _ in range(3):
        spine._on_tick()
    app.run_worker = original  # type: ignore[method-assign]
    spine._on_tick()
    import time as _t
    waited = 0.0
    while not provider_calls and waited < 2.0:
        _t.sleep(0.01)
        waited += 0.01
    assert provider_calls, "the slot must be reusable after a dispatch failure"


# -- forced refresh (the audited-write hook) --------------------------------


def test_force_refresh_polls_every_panel_immediately() -> None:
    """After an audited delete the wall must reflect it without waiting
    for the x15 services window: a forced poll updates EVERY panel."""
    app = _FakeApp()
    recorder = _Recorder()
    spine = RefreshSpine(app, lambda: demo_snapshot(1_000.0),
                         clock=lambda: 1_000.0)
    spine.subscribe(recorder)
    assert spine.force_refresh() is True, "started immediately"
    assert _wait_until(lambda: all(
        count >= 1 for count in spine.update_counts.values()))
    assert spine.update_counts["services"] == 1, (
        "the x15 panel refreshed without a single tick")
    assert recorder.frames[0][1] == frozenset(PANEL_MULTIPLIERS)


def test_force_refresh_queues_behind_an_in_flight_poll() -> None:
    """Single flight still holds: a force while a poll is mid-air is
    honored right after that poll lands — one extra full refresh, never
    an overlap."""
    app = _FakeApp()
    spine = RefreshSpine(app, lambda: demo_snapshot(1_000.0),
                         clock=lambda: 1_000.0)
    assert spine._flight.try_start(), "simulate a mid-air poll"
    assert spine.force_refresh() is False, "queued, not overlapped"
    assert spine._force_pending
    assert spine._flight.try_start() is False, "still only one in the air"
    spine._apply_snapshot(demo_snapshot(1_000.0))  # the mid-air poll lands
    assert _wait_until(lambda: spine.update_counts["services"] == 2), (
        "the queued force ran as its own full poll after landing")
    assert _wait_until(lambda: not spine.in_flight)
    assert not spine._force_pending, "queue drained"


def test_forced_poll_never_runs_concurrently_with_a_slow_provider() -> None:
    release = threading.Event()
    calls = {"n": 0, "live": 0, "max_live": 0}
    lock = threading.Lock()

    def gated_provider() -> WorkstationSnapshot:
        with lock:
            calls["n"] += 1
            calls["live"] += 1
            calls["max_live"] = max(calls["max_live"], calls["live"])
            first = calls["n"] == 1
        if first:
            release.wait(10.0)
        with lock:
            calls["live"] -= 1
        return demo_snapshot(1_000.0)

    app = _FakeApp()
    spine = RefreshSpine(app, gated_provider, clock=lambda: 1_000.0)
    spine._on_tick()
    assert spine.in_flight
    assert spine.force_refresh() is False, "queued behind the slow poll"
    release.set()
    assert _wait_until(lambda: calls["n"] == 2), (
        "the queued force ran as its own poll after the first landed")
    assert _wait_until(lambda: not spine.in_flight)
    assert calls["max_live"] == 1, "the provider never ran concurrently"


# ── SlowLane: the M6 screen-owned bounded poll lane ────────────────────


class _FakeScreen:
    """The SlowLane host duck type (run_worker/call_from_thread/is_mounted).

    ``app`` is the screen itself (the lane reaches the UI thread through
    ``screen.app.call_from_thread``).
    """

    def __init__(self) -> None:
        self.is_mounted = True
        self.landed: list[object] = []
        self.dispatch_failed = False
        self.app = self
        #: When set, UI-thread landings queue instead of running inline
        #: (mimicking call_from_thread onto a busy UI thread).
        self.defer = False
        self.pending: list[tuple[Callable[..., object], tuple[object, ...]]] = []

    def run_worker(self, work, **kwargs) -> None:
        if self.dispatch_failed:
            raise RuntimeError("no worker pool")
        threading.Thread(target=work).start()

    def call_from_thread(self, callback, *args) -> None:
        if self.defer:
            self.pending.append((callback, args))
        else:
            callback(*args)

    def flush(self) -> None:
        pending, self.pending = self.pending, []
        for callback, args in pending:
            callback(*args)


def test_slow_lane_never_overlaps() -> None:
    screen = _FakeScreen()
    lane = SlowLane()
    inside = threading.Event()
    release = threading.Event()
    live = {"n": 0, "max": 0}
    lock = threading.Lock()

    def work():
        with lock:
            live["n"] += 1
            live["max"] = max(live["max"], live["n"])
        inside.set()
        release.wait(5.0)
        with lock:
            live["n"] -= 1
        return {"ok": True}

    assert lane.run(screen, work, lambda r: None)
    assert inside.wait(5.0)
    assert lane.in_flight
    assert lane.run(screen, work, lambda r: None) is False, (
        "second poll refused while the first is in the air")
    release.set()
    assert _wait_until(lambda: not lane.in_flight)
    assert live["max"] == 1


def test_slow_lane_drops_stale_generations_and_unmounted_screens() -> None:
    screen = _FakeScreen()
    screen.defer = True  # landings queue like onto a busy UI thread
    lane = SlowLane()

    assert lane.run(screen, lambda: "old", lambda r: screen.landed.append(r))
    assert _wait_until(lambda: not lane.in_flight), (
        "worker 1 finished; its LANDING is still queued")
    # Gen 2 starts before gen 1's landing ran: the generation bumped.
    assert lane.run(screen, lambda: "new", lambda r: screen.landed.append(r))
    assert _wait_until(lambda: not lane.in_flight)
    screen.flush()
    assert screen.landed == ["new"], (
        "the superseded generation's result never lands")
    # A popped screen: is_mounted False swallows the delivery.
    screen.is_mounted = False
    assert lane.run(screen, lambda: "late", lambda r: screen.landed.append(r))
    assert _wait_until(lambda: not lane.in_flight)
    screen.flush()
    assert "late" not in screen.landed


def test_slow_lane_degrades_a_raising_work_and_a_failed_dispatch() -> None:
    screen = _FakeScreen()
    lane = SlowLane()

    def boom():
        raise RuntimeError("wedged transport")

    delivered: list[object] = []
    assert lane.run(screen, boom, delivered.append)
    assert _wait_until(lambda: len(delivered) == 1)
    assert delivered[0] == {"ok": False, "error": (
        "service error: RuntimeError('wedged transport')")}, (
        "a raising work degrades to the standard error result")
    assert not lane.in_flight, "the slot freed despite the raise"

    screen.dispatch_failed = True
    assert lane.run(screen, lambda: 1, delivered.append) is False
    assert not lane.in_flight, "failed dispatch releases the slot"
