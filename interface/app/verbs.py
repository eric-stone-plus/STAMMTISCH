"""The RUNS-table interaction verbs — the M3 grammar as pure logic.

The MOTOKO-proven verb set, adapted to this workstation's snapshot
contract (grilling S3: filter/sort/follow must never remove-then-reappend
rows, cursor identity is the run id, never the row index):

- ``/``  filter the table by a case-insensitive regex over run id +
  pipeline + state + the plain flags cell. An invalid pattern keeps the
  previous filter (the caller notifies); hidden rows stay hidden across
  refreshes; an empty submit or Esc clears.
- ``n``/``N``  next/previous match with wraparound. Every visible row
  matches an active filter by construction, so stepping walks the
  visible rows; the caller notifies when no filter is set.
- ``f``  follow toggle: the cursor rides its ROW KEY (the run id) across
  refreshes, sort rebuilds and filter hide/show. The latch survives the
  followed row being hidden; it re-anchors when the row returns; a
  clamped cursor never re-latches the latch. The latch is dropped only
  when the run leaves the snapshot entirely.
- ``s``  sort cycle: ``id`` / ``created`` / ``state-group``. Sorting runs
  on the snapshot's plain fields — rich Text cells are unorderable — and
  the caller applies it as ONE table rebuild with row keys preserved.

:class:`RunsView` is the whole state machine and deliberately imports no
Textual: :mod:`interface.screens.overview` binds it to the DataTable and
the tests drive it headlessly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from interface.render.flags import letter_flags
from interface.snapshot import TERMINAL_RUN_STATES, RunSnapshot, run_flags

__all__ = [
    "DEFAULT_SORT",
    "SORT_MODES",
    "RunsView",
    "run_search_text",
]

#: The sort cycle, in order (``s`` advances by one, wrapping).
SORT_MODES: tuple[str, ...] = ("id", "created", "state-group")

#: The default mode reproduces the collector's frame order (live states
#: first, frame order within each group) so the wall does not reshuffle
#: on the first keystroke.
DEFAULT_SORT = "state-group"


def run_search_text(
    run: RunSnapshot, *, feed_stale: bool = False, degraded: bool = False
) -> str:
    """The ``/`` filter's haystack: id + pipeline + state + flags cell."""
    flags = letter_flags(
        run_flags(run, feed_stale=feed_stale, degraded=degraded), styled=False
    )
    return f"{run.id} {run.pipeline_id} {run.state} {flags}"


def _state_group(run: RunSnapshot) -> int:
    """0 for live-ish states, 1 for terminal AND corrupt (sort_runs parity)."""
    return 1 if run.state in TERMINAL_RUN_STATES or run.state == "corrupt" else 0


class RunsView:
    """Filter + sort + follow state for the runs table (Textual-free)."""

    def __init__(self) -> None:
        self._filter_pattern: str | None = None
        self._regex: re.Pattern[str] | None = None
        self._sort_mode: str = DEFAULT_SORT
        self._followed_id: str | None = None

    # -- filter ------------------------------------------------------------

    @property
    def filter_pattern(self) -> str | None:
        """The active pattern (None when unfiltered)."""
        return self._filter_pattern

    @property
    def has_filter(self) -> bool:
        return self._regex is not None

    def set_filter(self, pattern: str) -> str | None:
        """Compile and install a filter; return an error string on a bad
        regex (the previous filter stays active), else ``None``."""
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return f"invalid regex: {exc}"
        self._filter_pattern = pattern
        self._regex = regex
        return None

    def clear_filter(self) -> None:
        self._filter_pattern = None
        self._regex = None

    def matches(
        self, run: RunSnapshot, *, feed_stale: bool = False, degraded: bool = False
    ) -> bool:
        """True when the run passes the active filter (always when none)."""
        if self._regex is None:
            return True
        haystack = run_search_text(run, feed_stale=feed_stale, degraded=degraded)
        return self._regex.search(haystack) is not None

    # -- sort ----------------------------------------------------------------

    @property
    def sort_mode(self) -> str:
        return self._sort_mode

    def cycle_sort(self) -> str:
        """Advance one step in :data:`SORT_MODES`; return the new mode."""
        index = SORT_MODES.index(self._sort_mode)
        self._sort_mode = SORT_MODES[(index + 1) % len(SORT_MODES)]
        return self._sort_mode

    def ordered_runs(self, runs: Sequence[RunSnapshot]) -> tuple[RunSnapshot, ...]:
        """The snapshot's runs in this view's sort order (plain fields only)."""
        ordered = list(runs)
        if self._sort_mode == "id":
            ordered.sort(key=lambda run: run.id)
        elif self._sort_mode == "created":
            ordered.sort(key=lambda run: (run.created_at, run.id), reverse=True)
        else:  # state-group: stable partition, frame order within each group
            ordered.sort(key=_state_group)
        return tuple(ordered)

    def visible_runs(
        self,
        runs: Sequence[RunSnapshot],
        *,
        feed_stale: bool = False,
        degraded: bool = False,
    ) -> tuple[RunSnapshot, ...]:
        """Ordered AND filtered — exactly the rows the table should show."""
        ordered = self.ordered_runs(runs)
        return tuple(
            run for run in ordered
            if self.matches(run, feed_stale=feed_stale, degraded=degraded)
        )

    # -- follow ----------------------------------------------------------------

    @property
    def followed_id(self) -> str | None:
        """The explicitly followed run id (the feed and cursor latch)."""
        return self._followed_id

    def toggle_follow(self, cursor_id: str | None) -> bool:
        """Latch onto the cursor's row key; toggles off when already on it.

        Returns the new following state. A ``None`` cursor can only clear.
        """
        if cursor_id is not None and self._followed_id != cursor_id:
            self._followed_id = cursor_id
            return True
        self._followed_id = None
        return False

    def retain_followed(self, present_ids: Iterable[str]) -> None:
        """Drop the latch only when the run leaves the snapshot entirely.

        A row hidden by the filter is still present: the latch survives
        (grilling S3) and re-anchors when the row becomes visible again.
        """
        if self._followed_id is not None and self._followed_id not in present_ids:
            self._followed_id = None

    # -- cursor policy -----------------------------------------------------

    def anchor_key(
        self, cursor_key: str | None, visible_keys: Sequence[str]
    ) -> str | None:
        """The row key the cursor should ride after a structural change.

        The follow latch wins when its row is visible; otherwise the
        cursor's own key survives if it can. A clamped cursor NEVER
        re-latches the latch — a hidden followed row keeps its latch.
        """
        if self._followed_id is not None and self._followed_id in visible_keys:
            return self._followed_id
        if cursor_key in visible_keys:
            return cursor_key
        return None

    def step_match(
        self,
        visible_keys: Sequence[str],
        cursor_key: str | None,
        *,
        backward: bool = False,
    ) -> str | None:
        """Next/previous match with wraparound (``n``/``N``).

        With a filter active every visible row is a match by construction,
        so this walks the visible rows cyclically. A cursor that is not in
        the visible set enters from the far end.
        """
        if not visible_keys:
            return None
        if cursor_key in visible_keys:
            index = visible_keys.index(cursor_key)
            offset = -1 if backward else 1
            return visible_keys[(index + offset) % len(visible_keys)]
        return visible_keys[-1 if backward else 0]
