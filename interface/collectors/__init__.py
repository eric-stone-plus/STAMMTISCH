"""Collector package — the data layer behind the snapshot contract.

``build_provider`` is the single entry point every tier consumes. The
deterministic demo collector stays available for UI work without the
core; the real path is a long-lived :class:`CollectorSession` (one per
``(root, use_core)``) whose events.jsonl byte cursors survive ticks, so
live frames cost one directory scan plus the appended bytes.

Every collector degrades: a missing state root yields empty facts with
a ``collector_error`` — nothing raises into the UI.
"""

from __future__ import annotations

import time
from pathlib import Path

from interface.collectors.demo import demo_snapshot
from interface.collectors.session import session_for
from interface.snapshot import SnapshotProvider, WorkstationSnapshot

__all__ = ["build_provider", "demo_snapshot", "session_for"]


def build_provider(root: Path | None, demo: bool) -> SnapshotProvider:
    """Resolve the snapshot provider for this process.

    ``demo=True`` ignores ``root`` and returns the deterministic synthetic
    collector. Otherwise ``root`` is the STAMMTISCH state root whose
    ``runs/`` holds the run directories, and the provider is backed by
    the process-wide session for that root (shared cursors across every
    provider over the same root). ``demo=False`` with no root degrades
    loudly rather than faking data.
    """
    if demo:
        return demo_snapshot
    if root is None:
        return lambda: WorkstationSnapshot(
            taken_at=time.time(),
            collector_error=(
                "no STAMMTISCH state root found — pass --root or set "
                "STAMMTISCH_HOME, or use --demo for synthetic data"),
        )
    session = session_for(root, use_core=True)
    return session.snapshot
