"""Tier 2 — the Rich Live rotator (``python -m interface.watch``).

A watch-mode glance over the same pure renderers as the TUI wall
(:mod:`interface.render.panels`): two pages (overview, runs) rotating
slowly, single-key control. The poll-loop core (:class:`WatchLoop`) is
stdlib-only and render-injected, so it stays testable without rich and
without a terminal; the rich renderer is a thin adapter around ``Live``.

Degradation contract:

- provider stall/raise: the last frame stays on screen plus a STALE
  banner (staleness levels come from the spine's shared arithmetic,
  :func:`interface.app.spine.staleness_for` — one definition everywhere).
- non-tty stdin/stdout (piped usage): print ONE status frame via tier 1
  (:func:`interface.status.render_status`) and exit 0 — the grep-safe
  glance. One one-shot renderer for the whole workstation (grilling S6),
  not two; the rich page below belongs to the interactive tty loop only.
- no rich installed: same tier-1 frame (the rich import is what fails).
- ``q`` / Ctrl-C: clean exit (terminal state restored).

Keys (the M5 minimal interaction set):

- ``p``      pause / resume AUTO-rotation. Paused still polls and still
  renders: data keeps aging, so a stalled provider still shows the STALE
  banner while the header says PAUSED; manual keys keep working.
- ``1``..``9`` jump straight to a page by number (out-of-range digits
  are ignored).
- space / Enter still advance one page (works while paused too; both
  reset the rotation clock so unpausing does not instantly rotate).
- ``q`` / Ctrl-C quit.
"""

from __future__ import annotations

import argparse
import select
import sys
import termios
import time
import tty
from collections.abc import Callable

from interface.app.spine import staleness_for
from interface.args import add_data_args, resolve_state_root
from interface.collectors import build_provider
from interface.snapshot import SnapshotProvider, WorkstationSnapshot

__all__ = ["WatchLoop", "main"]

#: The rotation: overview wall, then the runs page (table + event tail).
PAGES: tuple[str, ...] = ("overview", "runs")

# Lone ESC must NOT quit (review B: arrow keys send ESC-prefixed sequences,
# so a bare 0x1b byte read kills the rotator). q and Ctrl-C only.
_QUIT_KEYS = frozenset({"q", "\x03"})
_NEXT_PAGE_KEYS = frozenset({" ", "\n", "\r"})
_PAUSE_KEYS = frozenset({"p", "P"})
_FEED_PAGE_LINES = 12


class WatchLoop:
    """Stdlib poll-loop core: fetch, keep-last-frame, rotate, delegate.

    ``renderer(page, frame, stale_level, error, paused)`` draws;
    ``key_reader()`` returns one key or ``None`` (non-blocking);
    ``sleeper(seconds)`` waits. All four are injectable, which is what
    keeps this class honest under tests. The loop never raises: provider
    failures degrade to the STALE banner, ``KeyboardInterrupt`` exits
    cleanly with 0. Pause stops AUTO-rotation only — polling, staleness
    and manual keys keep working (a PAUSED stall still shows STALE).
    """

    def __init__(
        self,
        provider: SnapshotProvider,
        *,
        interval_s: float = 1.0,
        rotate_s: float = 15.0,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        key_reader: Callable[[], str | None] | None = None,
        renderer: Callable[[str, WorkstationSnapshot, str, str | None, bool],
                           object]
        | None = None,
        max_frames: int | None = None,
    ) -> None:
        self._provider = provider
        self._interval_s = interval_s
        self._rotate_s = rotate_s
        self._clock = clock
        self._sleeper = sleeper
        self._key_reader = key_reader
        self._renderer = renderer
        self._max_frames = max_frames
        self._frame: WorkstationSnapshot | None = None
        self._page_index = 0
        # None until run() starts: the first rotation window is measured
        # from loop start, not from the epoch (time.time()-scale clocks
        # would otherwise flip the page after ONE interval).
        self._last_rotate: float | None = None
        self._paused = False
        self.frames_rendered = 0
        self.provider_calls = 0

    @property
    def page(self) -> str:
        return PAGES[self._page_index]

    @property
    def paused(self) -> bool:
        """Whether AUTO-rotation is suspended (``p`` toggles)."""
        return self._paused

    def run(self) -> int:
        """Poll until quit key, Ctrl-C, or ``max_frames``; return exit 0."""
        try:
            self._last_rotate = self._clock()
            while True:
                self._render_one_frame()
                if (self._max_frames is not None
                        and self.frames_rendered >= self._max_frames):
                    return 0
                if self._read_key_once():
                    return 0
                self._sleeper(self._interval_s)
                self._maybe_rotate()
        except KeyboardInterrupt:
            return 0

    def _render_one_frame(self) -> None:
        error: str | None = None
        try:
            self.provider_calls += 1
            self._frame = self._provider()
        except Exception as exc:  # noqa: BLE001 - degrade: keep last frame
            error = f"provider error: {exc!r}"
        frame = self._frame
        if frame is None:
            frame = WorkstationSnapshot(taken_at=self._clock(),
                                        collector_error=error or "no frame yet")
        age_s = max(0.0, self._clock() - frame.taken_at)
        stale = "crit" if error else staleness_for(
            age_s, multiplier=1, interval_s=self._interval_s
        )
        if self._renderer is not None:
            self._renderer(self.page, frame, stale, error, self._paused)
        self.frames_rendered += 1

    def _read_key_once(self) -> bool:
        """Consume one buffered key; return True when the loop must stop."""
        if self._key_reader is None:
            return False
        key = self._key_reader()
        if key in _QUIT_KEYS:
            return True
        if key in _NEXT_PAGE_KEYS:
            self._advance_page()
        elif key in _PAUSE_KEYS:
            self._paused = not self._paused
            # Resume gets a FRESH window: a long pause must not flip the
            # page within one tick of unpause.
            self._last_rotate = self._clock()
        elif key is not None and key.isdigit() and key != "0":
            self._jump_page(int(key))
        return False

    def _maybe_rotate(self) -> None:
        if self._paused or self._last_rotate is None:
            return
        if self._clock() - self._last_rotate >= self._rotate_s:
            self._advance_page()

    def _advance_page(self) -> None:
        self._page_index = (self._page_index + 1) % len(PAGES)
        self._last_rotate = self._clock()

    def _jump_page(self, number: int) -> bool:
        """Digits 1..N jump straight to a page; out-of-range is ignored.

        A jump resets the rotation clock (like every manual page change),
        so the page the operator chose stays up for a full window.
        """
        index = number - 1
        if not 0 <= index < len(PAGES):
            return False
        self._page_index = index
        self._last_rotate = self._clock()
        return True


class _TtyKeys:
    """Single-key cbreak reader; terminal state restored on close."""

    def __init__(self) -> None:
        self._fd = sys.stdin.fileno()
        self._attrs = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)

    def read(self) -> str | None:
        ready, _, _ = select.select([sys.stdin], [], [], 0)
        if not ready:
            return None
        return sys.stdin.read(1)

    def close(self) -> None:
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._attrs)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="interface.watch",
        description="Watch the STAMMTISCH workstation rotate. "
        "p pauses auto-rotation, 1..9 jumps pages, space advances, "
        "q or Ctrl-C quits.",
    )
    add_data_args(parser)
    parser.add_argument("--interval", type=float, default=1.0, metavar="SEC",
                        help="poll interval in seconds (default 1.0)")
    parser.add_argument("--rotate", type=float, default=15.0, metavar="SEC",
                        help="seconds per page before auto-rotating "
                             "(default 15.0)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the watch rotator; every one-shot path is tier 1 (S6 dedupe)."""
    args = _parse_args(argv)
    provider = build_provider(resolve_state_root(args.root), demo=args.demo)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # Piped usage: one tier-1 frame, then exit — the cron/SSH glance.
        from interface.status import render_status

        sys.stdout.write(render_status(provider()))
        return 0
    try:
        from rich.columns import Columns
        from rich.console import Console, Group
        from rich.live import Live
        from rich.text import Text

        from interface.render import panels
        from interface.render.tokens import token
    except ImportError:  # no rich in the environment: tier 1 one-shot
        from interface.status import render_status

        sys.stdout.write(render_status(provider()))
        return 0

    def _header(frame: WorkstationSnapshot, stale: str,
                error: str | None, paused: bool) -> Text:
        header = panels.banner(frame)
        if paused:
            header.append("  PAUSED", style=f"bold {token('state.warn')}")
        if stale == "crit":
            header.append("  STALE", style=f"bold {token('stale.crit')}")
        elif stale == "warn":
            header.append("  stale", style=token("stale.warn"))
        if error:
            header.append(f"  {error}", style=token("state.crit"))
        return header

    def _page_body(page: str, frame: WorkstationSnapshot) -> object:
        if page == "runs":
            run = panels.followed_run(frame)
            if run is None or not run.events_tail:
                lines: list[Text] = [
                    Text("(no events)", style=token("text.muted"))
                ]
            else:
                lines = [panels.feed_line(event)
                         for event in run.events_tail[-_FEED_PAGE_LINES:]]
            return Group(panels.runs_table(frame), *lines)
        side = Columns(
            [panels.glance_block(frame.quotes),
             panels.services_strip(frame.services)],
            equal=True, expand=True,
        )
        return Group(panels.runs_table(frame), side)

    def render_page(
        page: str, frame: WorkstationSnapshot, stale: str, error: str | None,
        paused: bool,
    ) -> object:
        return Group(_header(frame, stale, error, paused),
                     _page_body(page, frame))

    console = Console()
    keys = _TtyKeys()
    try:
        with Live(console=console, refresh_per_second=4) as live:
            loop = WatchLoop(
                provider,
                interval_s=args.interval,
                rotate_s=args.rotate,
                key_reader=keys.read,
                renderer=lambda page, frame, stale, error, paused: live.update(
                    render_page(page, frame, stale, error, paused)
                ),
            )
            return loop.run()
    finally:
        keys.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
