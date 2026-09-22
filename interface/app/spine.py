"""The refresh spine — one clock, per-panel cadence, single-flight fetch.

Every frame the workstation shows crosses this module exactly once. The
cadence rules (the MOTOKO lesson, restated):

- ONE global 1s tick; every panel refreshes only on its own multiple
  (``PANEL_MULTIPLIERS``). A panel refreshed every frame is a bug, and
  ``interface/tests/test_cadence_counts.py`` exists to catch it.
- STALE is data, not style: the spine exposes per-panel staleness levels
  (``fresh`` / ``warn`` / ``crit``) computed from data age vs the panel's
  multiplier; screens apply the styling and the STALE badge.
- Boot hydration: until the runs table has rows — or a frame legitimately
  reports zero runs — every frame updates every panel. The latch never
  re-arms on later empty frames.
- Single flight: a poll starts only when the previous poll finished.
  Cancelling a Textual worker cannot stop its thread, so the spine simply
  never overlaps them.
- Frames cross from worker threads into the UI via ``app.call_from_thread``
  onto ONE mutation path (``_apply_snapshot``).

The scheduling core (:class:`Cadence`, :class:`SingleFlight`,
:class:`SlowLane`, :class:`ErrorBell`, :func:`staleness_for`) is pure and
headless-testable; :class:`RefreshSpine` adds the Textual timer/worker
wiring and is the only part that needs a live app. This module must stay
importable without Textual (tier 2 reuses the pure parts), so Textual
types are imported under ``TYPE_CHECKING`` only.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from interface.snapshot import WorkstationSnapshot

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from textual.app import App

__all__ = [
    "PANEL_MULTIPLIERS",
    "Cadence",
    "ErrorBell",
    "RefreshSpine",
    "SingleFlight",
    "SlowLane",
    "staleness_for",
]

#: Panel ids of this workstation -> refresh multiplier in ticks. The 1s
#: spine tick is the base unit: the activity feed and the banner (flag/
#: clock strip) refresh every tick, the runs table every 2nd, the glance
#: quotes every 5th, the services banner every 15th.
PANEL_MULTIPLIERS: Mapping[str, int] = {
    "feed": 1,
    "banner": 1,
    "runs": 2,
    "glance": 5,
    "services": 15,
}

#: Staleness semantics in multiples of the panel's own refresh period:
#: data older than ``WARN_FACTOR`` periods is warn, ``CRIT_FACTOR`` is
#: crit (plus the STALE badge the screens render).
WARN_FACTOR = 3
CRIT_FACTOR = 10


def staleness_for(age_s: float, multiplier: int, *, interval_s: float = 1.0) -> str:
    """Staleness level of data ``age_s`` seconds old for one panel.

    Pure arithmetic, shared by every tier; screens map the returned level
    to styles, never to thresholds of their own.
    """
    period_s = multiplier * interval_s
    if age_s > CRIT_FACTOR * period_s:
        return "crit"
    if age_s > WARN_FACTOR * period_s:
        return "warn"
    return "fresh"


class Cadence:
    """Pure tick/counter core: which panels are due on which tick.

    Drivable headlessly: ``tick()`` returns the set of due panel ids and
    ``observe_frame()`` feeds the boot hydration latch. Panel ids come
    from the multiplier mapping handed in (defaults:
    :data:`PANEL_MULTIPLIERS`).
    """

    def __init__(self, multipliers: Mapping[str, int] | None = None) -> None:
        self._multipliers: dict[str, int] = dict(
            multipliers if multipliers is not None else PANEL_MULTIPLIERS
        )
        self._ticks = 0
        self._hydrated = False

    @property
    def panel_ids(self) -> tuple[str, ...]:
        return tuple(self._multipliers)

    @property
    def tick_count(self) -> int:
        return self._ticks

    @property
    def hydrated(self) -> bool:
        return self._hydrated

    def multiplier(self, panel: str) -> int:
        """The refresh multiplier of one panel (1 for unknown panels)."""
        return self._multipliers.get(panel, 1)

    def tick(self) -> frozenset[str]:
        """Advance one tick; return the panel ids due for refresh."""
        self._ticks += 1
        if not self._hydrated:
            return frozenset(self._multipliers)
        return frozenset(
            panel
            for panel, multiplier in self._multipliers.items()
            if self._ticks % multiplier == 0
        )

    def observe_frame(self, snapshot: WorkstationSnapshot) -> bool:
        """Feed one applied frame to the hydration latch.

        The latch disarms on the first frame whose runs table has rows, or
        on the first frame that legitimately reports zero runs (a healthy
        collector with no ``collector_error``). It never re-arms: once
        boot hydration is complete, later empty frames are rendered as
        data, not as boot. Returns ``True`` iff this call disarmed it.
        """
        if self._hydrated:
            return False
        if snapshot.runs or snapshot.collector_error is None:
            self._hydrated = True
            return True
        return False


class SingleFlight:
    """One poll in the air at a time (cancel cannot stop a worker thread).

    Thread-safe by construction (D-M6 finding 2): ``try_start`` claims
    the slot on the UI thread while ``finish()`` releases it from WORKER
    threads — a lock-free check-then-set would not be atomic, so both
    (and the ``in_flight`` read) go through one :class:`threading.Lock`.
    """

    def __init__(self) -> None:
        self._in_flight = False
        self._lock = threading.Lock()

    @property
    def in_flight(self) -> bool:
        with self._lock:
            return self._in_flight

    def try_start(self) -> bool:
        """Claim the poll slot; ``False`` means a poll is still running."""
        with self._lock:
            if self._in_flight:
                return False
            self._in_flight = True
            return True

    def finish(self) -> None:
        with self._lock:
            self._in_flight = False


class SlowLane:
    """One bounded service-poll lane owned by a screen (M6).

    The spine's single-flight discipline applied to a screen's OWN slow
    service calls (the absorbed read-only tapes): never overlap — a lane
    poll starts only when the previous one finished — and drop stale — a
    generation counter ensures a late result from a popped or re-kicked
    screen never lands over a newer one. The work runs on a Textual
    thread worker and lands on the UI thread via ``call_from_thread``
    exactly once, through ``deliver``.

    The host is duck-typed (any Textual Screen satisfies it:
    ``run_worker``/``app.call_from_thread``/``is_mounted``), so the class
    stays Textual-free and headless-testable by calling :meth:`_land`
    directly. A raising ``run_worker`` releases the slot (same guard as
    the spine's dispatch).
    """

    def __init__(self) -> None:
        self._flight = SingleFlight()
        self._generation = 0
        self._lock = threading.Lock()

    @property
    def in_flight(self) -> bool:
        return self._flight.in_flight

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def run(self, screen: Any, work: Callable[[], Any],
            deliver: Callable[[Any], None], *, name: str = "slow-lane") -> bool:
        """Start one lane poll; ``False`` (never raising) when one runs.

        ``work`` executes on the worker thread and should degrade rather
        than raise; if it raises anyway, the lane delivers the standard
        ``{"ok": False, "error": ...}`` degradation instead.
        """
        if not self._flight.try_start():
            return False
        with self._lock:
            self._generation += 1
            generation = self._generation

        def _body() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - degrade, never raise
                result = {"ok": False, "error": f"service error: {exc!r}"}
            finally:
                self._flight.finish()
            try:
                screen.app.call_from_thread(
                    self._land, screen, generation, result, deliver)
            except Exception:  # noqa: BLE001, S110 - app torn down; land quietly
                pass

        try:
            screen.run_worker(_body, name=name, group="slow-lane", thread=True)
        except Exception:  # noqa: BLE001 - dispatch failed: release the slot
            self._flight.finish()
            return False
        return True

    def _land(self, screen: Any, generation: int, result: Any,
              deliver: Callable[[Any], None]) -> None:
        """UI-thread landing: mounted + current generation only."""
        if not screen.is_mounted:
            return
        with self._lock:
            if generation != self._generation:
                return  # a newer poll superseded this one: drop it
        deliver(result)


class ErrorBell:
    """Ring once per collector-error episode (rising edge only)."""

    def __init__(self) -> None:
        self._in_error = False

    @property
    def in_error(self) -> bool:
        return self._in_error

    def observe(self, has_error: bool) -> bool:
        """Record the frame's error state; ``True`` iff the bell rings."""
        rings = has_error and not self._in_error
        self._in_error = has_error
        return rings


class RefreshSpine:
    """Wires the pure core onto a Textual app: timer, worker, one path in.

    The app only needs ``set_interval``, ``run_worker(..., thread=True)``,
    ``call_from_thread`` and ``bell`` — which is why tests can drive this
    class with a small fake and no live Textual app.
    """

    def __init__(
        self,
        app: App[None],
        provider: Callable[[], WorkstationSnapshot],
        *,
        interval_s: float = 1.0,
        clock: Callable[[], float] = time.time,
        multipliers: Mapping[str, int] | None = None,
    ) -> None:
        self._app = app
        self._provider = provider
        self._interval_s = interval_s
        self._clock = clock
        self._cadence = Cadence(multipliers)
        self._flight = SingleFlight()
        self._bell = ErrorBell()
        self._last_frame: WorkstationSnapshot | None = None
        self._pending_due: frozenset[str] = frozenset()
        self._update_counts: dict[str, int] = {p: 0 for p in self._cadence.panel_ids}
        self._stale_levels: dict[str, str] = {p: "fresh" for p in self._cadence.panel_ids}
        self._subscribers: list[object] = []
        self._timer = None
        self._force_pending = False

    # -- observability (tests and the screen read these) -----------------

    @property
    def update_counts(self) -> Mapping[str, int]:
        """Panels actually updated through ``_apply_snapshot``, per id."""
        return self._update_counts

    @property
    def tick_count(self) -> int:
        return self._cadence.tick_count

    @property
    def last_frame(self) -> WorkstationSnapshot | None:
        return self._last_frame

    @property
    def stale_levels(self) -> Mapping[str, str]:
        """Current per-panel staleness (``fresh``/``warn``/``crit``)."""
        return self._stale_levels

    @property
    def in_flight(self) -> bool:
        return self._flight.in_flight

    # -- subscription -----------------------------------------------------

    def subscribe(self, listener: object) -> None:
        """Register a listener with ``spine_frame`` / ``spine_staleness``."""
        if listener not in self._subscribers:
            self._subscribers.append(listener)

    def unsubscribe(self, listener: object) -> None:
        if listener in self._subscribers:
            self._subscribers.remove(listener)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Start the global tick (idempotent)."""
        if self._timer is None:
            self._timer = self._app.set_interval(self._interval_s, self._on_tick)

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def force_refresh(self) -> bool:
        """Force ONE immediate full refresh (used after an audited write).

        Honors single flight: when a poll is already in the air the forced
        poll is QUEUED and runs right after that poll lands (honored in
        ``_apply_snapshot``), so forced refreshes never overlap either.
        Returns ``True`` when the forced poll started immediately.
        """
        self._pending_due = frozenset(self._cadence.panel_ids)
        if not self._flight.try_start():
            self._force_pending = True  # queued behind the in-flight poll
            return False
        self._force_pending = False
        self._dispatch_poll()
        return True

    # -- the tick ----------------------------------------------------------

    def _on_tick(self) -> None:
        self._tick_staleness()
        due = self._cadence.tick()
        if not due:
            return
        if not self._flight.try_start():
            return  # previous poll still running: never overlap
        self._pending_due = due
        self._dispatch_poll()

    def _dispatch_poll(self) -> None:
        """Start the fetch worker (the flight slot is already claimed)."""
        try:
            self._app.run_worker(
                self._fetch, name="spine-poll", group="spine", thread=True
            )
        except Exception:  # noqa: BLE001 - dispatch failed: release the slot
            # Adversarial review B: without this, a raising run_worker leaves
            # the flight slot claimed forever and the spine never polls
            # again. Swallow-and-release: the next tick retries the poll.
            self._flight.finish()

    def _tick_staleness(self) -> None:
        """Recompute staleness every tick (it grows between frames too)."""
        if self._last_frame is None:
            return
        age_s = max(0.0, self._clock() - self._last_frame.taken_at)
        levels = {
            panel: staleness_for(age_s, self._panel_multiplier(panel))
            for panel in self._cadence.panel_ids
        }
        if levels != self._stale_levels:
            self._stale_levels = levels
            for listener in tuple(self._subscribers):
                apply_staleness = getattr(listener, "spine_staleness", None)
                if apply_staleness is not None:
                    apply_staleness(levels)

    def _panel_multiplier(self, panel: str) -> int:
        return self._cadence.multiplier(panel)

    # -- the poll (worker thread) -------------------------------------------

    def _fetch(self) -> None:
        """Worker-thread body: one provider call, degraded to a frame."""
        try:
            frame = self._provider()
        except Exception as exc:  # noqa: BLE001 - degrade, never raise into the UI
            frame = WorkstationSnapshot(
                taken_at=self._clock(),
                collector_error=f"provider error: {exc!r}",
            )
        try:
            self._app.call_from_thread(self._apply_snapshot, frame)
        except Exception:  # noqa: BLE001 - app is shutting down; just land
            self._flight.finish()

    # -- ONE mutation path ----------------------------------------------------

    def _apply_snapshot(self, frame: WorkstationSnapshot) -> None:
        """The only place a frame mutates spine + UI state (UI thread)."""
        try:
            self._last_frame = frame
            self._cadence.observe_frame(frame)
            if self._bell.observe(frame.collector_error is not None):
                self._app.bell()
            due = self._pending_due
            for panel in due:
                if panel in self._update_counts:
                    self._update_counts[panel] += 1
            for listener in tuple(self._subscribers):
                receive = getattr(listener, "spine_frame", None)
                if receive is not None:
                    receive(frame, due)
        finally:
            self._flight.finish()
            if self._force_pending:
                # A forced refresh queued behind this poll: honor it now.
                self._force_pending = False
                self._pending_due = frozenset(self._cadence.panel_ids)
                if self._flight.try_start():
                    self._dispatch_poll()
