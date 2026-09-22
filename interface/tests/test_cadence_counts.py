"""THE counting test (the reviewed MOTOKO lesson: cadence claims drifted
twice, so the cadence is asserted, not claimed).

Runs the real app (spine timer + worker + screen) under a headless Pilot
with a fast 50ms tick and an instrumented demo provider, then asserts:

- updates per panel track the panel's multiplier (x1 every tick, x2 half
  the x1 rate, x5 a fifth, x15 a fifteenth) within tolerance — a bug that
  forces every panel every frame fails here by a wide margin;
- the screen applied exactly what the spine dispatched (no silent drops);
- the provider is called at most once per tick (single flight in the
  real wiring, not just in the pure unit).
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("textual", reason="UI tests need textual>=8")

from interface.app.shell import WorkstationShell
from interface.app.spine import PANEL_MULTIPLIERS
from interface.collectors.demo import demo_snapshot
from interface.snapshot import WorkstationSnapshot

_TICK_S = 0.05
_RUN_S = 3.0


def test_updates_per_panel_track_multipliers() -> None:
    """Live app: counts[m] ~= counts[1]/m for every panel multiplier m."""

    async def scenario() -> tuple[int, dict[str, int], dict[str, int], int]:
        state = {"t": 2_000.0, "calls": 0}

        def provider() -> WorkstationSnapshot:
            state["calls"] += 1
            state["t"] += 0.25
            return demo_snapshot(state["t"])

        def clock() -> float:
            return state["t"] + 0.125  # age stays fresh on the virtual clock

        app = WorkstationShell(provider=provider, interval_s=_TICK_S, clock=clock)
        async with app.run_test(size=(120, 40)):
            await asyncio.sleep(_RUN_S)
            spine_counts = dict(app.spine.update_counts)
            screen = app.screen
            screen_counts = dict(screen.update_counts)
        return (app.spine.tick_count, spine_counts, screen_counts,
                state["calls"])

    ticks, counts, screen_counts, calls = asyncio.run(scenario())

    assert ticks >= 20, f"spine starved: only {ticks} ticks in {_RUN_S}s"
    feed = counts["feed"]
    assert feed >= 15, "x1 feed panel must refresh essentially every tick"

    for panel, multiplier in PANEL_MULTIPLIERS.items():
        # Over T ticks a x1 panel updates ~T times and a xm panel ~T/m
        # times (both +1 from the boot hydration frame). The tolerance is
        # generous for scheduler jitter and ZERO for the every-frame bug:
        # forcing all panels each frame would put services at ~feed.
        expected = (feed - 1) / multiplier + 1
        tolerance = max(1.5, expected * 0.3)
        actual = counts[panel]
        assert abs(actual - expected) <= tolerance, (
            f"panel {panel!r} (x{multiplier}): {actual} updates, "
            f"expected ~{expected:.1f} (feed was {feed})"
        )

    # The screen applied what the spine dispatched (at most one frame of
    # subscription slack at startup, never more, never less).
    for panel in PANEL_MULTIPLIERS:
        assert screen_counts[panel] <= counts[panel] <= screen_counts[panel] + 1, (
            f"screen dropped or doubled {panel!r}: spine {counts[panel]} "
            f"vs screen {screen_counts[panel]}"
        )

    # Single flight in the real wiring: one provider call per tick, ever.
    assert calls <= ticks, f"provider called {calls} times over {ticks} ticks"
    assert calls >= ticks - 2, "provider should be polled nearly every tick"
