"""Watch-tier interaction tests (M5): pause, digit jumps, quit keys.

The WatchLoop core is driven headlessly with an injected clock, a no-op
sleeper that ADVANCES that clock (one loop iteration == one interval),
and a scriptable key reader — no rich, no Live, no terminal.
"""

from __future__ import annotations

from interface.collectors.demo import demo_snapshot
from interface.snapshot import WorkstationSnapshot
from interface.watch import PAGES, WatchLoop


class _Clock:
    """Injectable clock; ``tick()`` advances it by ``step``."""

    def __init__(self, start: float = 1_000.0, step: float = 1.0) -> None:
        self.now = start
        self.step = step

    def __call__(self) -> float:
        return self.now

    def tick(self) -> None:
        self.now += self.step


class _Keys:
    """Scriptable key reader: one queued key per loop iteration."""

    def __init__(self, script: list[str]) -> None:
        self._script = list(script)

    def __call__(self) -> str | None:
        return self._script.pop(0) if self._script else None


class _Recorder:
    """Renderer capture: one (page, stale, error, paused) per frame."""

    def __init__(self) -> None:
        self.frames: list[tuple[str, str, str | None, bool]] = []

    def __call__(self, page: str, frame: WorkstationSnapshot, stale: str,
                 error: str | None, paused: bool) -> None:
        self.frames.append((page, stale, error, paused))


def _loop(provider, keys: list[str], *, rotate_s: float = 15.0,
          frames: int, clock_step: float = 1.0):
    clock = _Clock(step=clock_step)
    recorder = _Recorder()
    loop = WatchLoop(
        provider,
        interval_s=1.0,
        rotate_s=rotate_s,
        clock=clock,
        sleeper=lambda _s: clock.tick(),
        key_reader=_Keys(keys),
        renderer=recorder,
        max_frames=frames,
    )
    return loop, recorder, clock


# ── pause: auto-rotation stops across two would-be rotate windows ────────


def test_pause_stops_rotation_across_two_rotate_windows() -> None:
    """``p`` freezes the page across >2 rotation windows; unpause resumes."""
    # 20 frames, rotate every 5s: unpaused it would rotate ~4 times.
    loop, recorder, _ = _loop(
        lambda: demo_snapshot(1_000.0),
        keys=["p"],  # pause at the first key read (iteration 2)
        rotate_s=5.0,
        frames=20,
    )
    assert loop.run() == 0
    pages = [page for page, _, _, _ in recorder.frames]
    assert recorder.frames[0][3] is False, "starts unpaused"
    assert recorder.frames[1][3] is True, "p paused at the first read"
    assert set(pages[1:]) == {PAGES[0]}, (
        f"paused never rotated, got {pages}")
    assert loop.paused is True


def test_unpause_resumes_rotation_from_a_full_window() -> None:
    """After unpause the page stays one full rotate window (no instant
    flip from the paused time), then rotates again."""
    loop, recorder, _ = _loop(
        lambda: demo_snapshot(1_000.0),
        keys=["p", "p"],  # pause, then unpause 1s later
        rotate_s=5.0,
        frames=12,
    )
    assert loop.run() == 0
    pages = [page for page, _, _, _ in recorder.frames]
    # Frame 1 paused; frame 2 unpaused but unpause reset the rotate clock,
    # so the flip lands only after another full 5s window.
    assert pages[2:6] == [PAGES[0]] * 4, (
        f"no instant rotation on unpause, got {pages}")
    assert PAGES[1] in pages[6:], "rotation resumed after unpause"
    assert loop.paused is False


# ── digit jumps and manual advance ───────────────────────────────────────


def test_digits_jump_straight_to_a_page() -> None:
    loop, recorder, _ = _loop(
        lambda: demo_snapshot(1_000.0),
        keys=["2", "1"],
        rotate_s=1_000.0,  # auto-rotation off by distance
        frames=6,
    )
    assert loop.run() == 0
    pages = [page for page, _, _, _ in recorder.frames]
    # Keys are read after each render: "2" lands on frame 2, "1" on frame 3.
    assert pages == [PAGES[0], PAGES[1], PAGES[0], PAGES[0], PAGES[0],
                     PAGES[0]], pages


def test_out_of_range_digit_is_ignored() -> None:
    loop, recorder, _ = _loop(
        lambda: demo_snapshot(1_000.0),
        keys=["9", "0"],
        rotate_s=1_000.0,
        frames=4,
    )
    assert loop.run() == 0
    assert {page for page, _, _, _ in recorder.frames} == {PAGES[0]}
    assert loop.page == PAGES[0]


def test_space_advances_while_paused_and_resets_the_window() -> None:
    """Manual keys keep working under pause, and the manual change resets
    the rotation clock (unpausing does not instantly rotate)."""
    loop, recorder, _ = _loop(
        lambda: demo_snapshot(1_000.0),
        keys=["p", " ", "p"],  # pause, advance, unpause
        rotate_s=5.0,
        frames=9,
    )
    assert loop.run() == 0
    pages = [page for page, _, _, _ in recorder.frames]
    assert pages[2] == PAGES[1], "space advanced while paused"
    # After unpause (frame 3) the flip from the manual advance holds for a
    # full 5s window before auto-rotation moves on.
    assert pages[3:7] == [PAGES[1]] * 4, pages


# ── PAUSED stall still shows the STALE banner ───────────────────────────


def test_paused_stall_still_shows_the_stale_banner() -> None:
    """Pause suspends rotation, never staleness: a provider that keeps
    returning an old frame degrades to crit while paused is on the wire."""

    frame = demo_snapshot(1_000.0)

    def stalling_provider() -> WorkstationSnapshot:
        return frame  # taken_at frozen at 1_000.0

    loop, recorder, _ = _loop(
        stalling_provider, keys=["p"], rotate_s=5.0, frames=15,
    )
    assert loop.run() == 0
    crit_paused = [(page, stale) for page, stale, _, paused in recorder.frames
                   if paused and stale == "crit"]
    assert crit_paused, "stale level kept rising while paused"
    assert crit_paused[0][0] == PAGES[0], "page frozen, staleness not"


def test_paused_provider_error_still_reaches_the_renderer() -> None:
    def broken_provider() -> WorkstationSnapshot:
        raise RuntimeError("core gone")

    loop, recorder, _ = _loop(
        broken_provider, keys=["p"], rotate_s=5.0, frames=4,
    )
    assert loop.run() == 0
    assert recorder.frames[-1][3] is True
    assert recorder.frames[-1][2] and "core gone" in recorder.frames[-1][2]
    assert recorder.frames[-1][1] == "crit"


# ── quit keys (regression B-obs neighborhood) ────────────────────────────


def test_quit_keys_are_exactly_q_and_ctrl_c() -> None:
    from interface.watch import _QUIT_KEYS

    assert _QUIT_KEYS == frozenset({"q", "\x03"}), (
        "lone ESC must never quit (arrow keys send ESC-prefixed sequences)")
    for quit_key in ("q", "\x03"):
        loop, recorder, _ = _loop(
            lambda: demo_snapshot(1_000.0), keys=[quit_key], frames=99,
        )
        assert loop.run() == 0
        assert len(recorder.frames) == 1, "rendered once, read key, stopped"


def test_esc_and_arrow_prefix_do_not_quit() -> None:
    loop, recorder, _ = _loop(
        lambda: demo_snapshot(1_000.0), keys=["\x1b", "\x1b[A", "x"],
        rotate_s=1_000.0, frames=5,
    )
    assert loop.run() == 0
    assert loop.paused is False
    assert len(recorder.frames) == 5, "unknown keys are ignored, not fatal"
    assert {page for page, _, _, _ in recorder.frames} == {PAGES[0]}
