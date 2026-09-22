"""CollectorSession — long-lived collectors assembled into whole frames.

One session per ``(root, use_core)`` owns the collector set for a
process: the events byte cursors (and every other incremental state)
survive ticks, so successive frames cost one directory scan plus the
bytes appended since the last frame. Each ``snapshot()`` assembles FRESH
contract containers — freeze is shallow, so freshness is collector
discipline, per the contract's known limit.

Frame assembly sorts runs live-states-first, newest ``created_at``
within each group. Quotes (M4) come from the TTL-batched feeds lane
over the services livefeed copy — per-symbol age + serving source, the
last good set kept (aging toward STALE) when a refresh fails. The demo
provider keeps its own synthetic quotes and never touches this session.
``collector_error`` joins the per-collector error strings (or None when
every collector is healthy). A missing core binary is a services row,
not a collector error.
"""

from __future__ import annotations

import time
from pathlib import Path

from interface.collectors.core_cli import CoreCliClient
from interface.collectors.events import EventsCollector
from interface.collectors.feeds import FeedsCollector
from interface.collectors.files import FilesCollector
from interface.services.glance import GLANCE_LABELS
from interface.snapshot import (
    TERMINAL_RUN_STATES,
    RunSnapshot,
    ServiceStatus,
    WorkstationSnapshot,
)

__all__ = ["CollectorSession", "session_for", "sort_runs"]


def sort_runs(runs: tuple[RunSnapshot, ...]) -> tuple[RunSnapshot, ...]:
    """Live states first, then newest ``created_at`` within each group."""
    ordered = list(runs)
    ordered.sort(key=lambda r: r.created_at, reverse=True)  # newest first
    # Stable second pass: live runs first; terminal AND corrupt runs behind
    # (review A note: corrupt used to sort with the live group).
    ordered.sort(key=lambda r: r.state in TERMINAL_RUN_STATES
                 or r.state == "corrupt")
    return tuple(ordered)


class CollectorSession:
    """The long-lived station of collectors behind one state root."""

    def __init__(self, root: Path, *, use_core: bool = True) -> None:
        self.root = Path(root)
        self.events = EventsCollector(self.root)
        self.files = FilesCollector(self.root)
        self.feeds = FeedsCollector(GLANCE_LABELS, labels=GLANCE_LABELS)
        self.core = CoreCliClient(root=self.root) if use_core else None

    def snapshot(self) -> WorkstationSnapshot:
        """Assemble one consistent frame; never raises into the UI."""
        runs, events_error = self.events.collect()
        intake, optional_services, files_error = self.files.collect()
        services: tuple[ServiceStatus, ...] = optional_services
        if self.core is not None:
            services = (self.core.service_status(), *services)
        errors = [e for e in (events_error, files_error) if e]
        return WorkstationSnapshot(
            taken_at=time.time(),
            runs=sort_runs(runs),
            intake=intake,
            quotes=self.feeds.collect(),
            services=services,
            collector_error="; ".join(errors) if errors else None,
        )


_SESSIONS: dict[tuple[str, bool], CollectorSession] = {}


def session_for(root: Path, *, use_core: bool = True) -> CollectorSession:
    """The process-wide session for ``(root, use_core)``.

    Keyed by resolved root so two providers over the same state root
    share one set of cursors; cursors survive ticks by construction.
    """
    key = (str(Path(root).resolve()), use_core)
    session = _SESSIONS.get(key)
    if session is None:
        session = CollectorSession(Path(root), use_core=use_core)
        _SESSIONS[key] = session
    return session
