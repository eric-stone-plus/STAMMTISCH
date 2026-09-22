"""FeedsHealthService — provider counters + lane cache, out of the screen.

M7 reconciliation: the per-provider health counters live ONCE in the
shared registry (``services/datafeeds/registry.py`` — the same
registry the shared chain's ``tracked`` calls report into), so the
interface's feeds lane and the FEEDS screen read the true counters
instead of a copy that could drift. This module adapts the shared
mutable rows into this tree's frozen contract and keeps what is
genuinely interface-native:

- the lane-cache split (:func:`note_cache` / :func:`cache_stats`) —
  the feeds collector's keep-last-good row set REPORTS its
  fresh/stale split here; the FEEDS screen answers "what is serving
  me right now", not "what is on disk";
- the latency bands (new, for the screen's marking): the EWMA
  average falls into ok/warn/crit bands — thresholds shared by every
  consumer, like the snapshot's other semantic thresholds.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from services.datafeeds import registry as _shared_registry

__all__ = [
    "LATENCY_CRIT_MS",
    "LATENCY_WARN_MS",
    "CacheHealth",
    "FeedHealthFrame",
    "FeedHealthService",
    "ProviderHealth",
    "all_stats",
    "latency_band",
    "note_cache",
    "reset_stats",
    "tracked",
]

#: Semantic latency bands for the FEEDS table (avg over the EWMA).
LATENCY_WARN_MS = 1_500.0
LATENCY_CRIT_MS = 4_000.0

_LOCK = threading.Lock()

#: Lane cache stats reported by the feeds collector (old cache_stats shape).
_CACHE: dict[str, Any] = {"entries": 0, "stale_entries": 0, "disk_dir": None}


@dataclass(frozen=True)
class ProviderHealth:
    """Rolling health for one named provider (shared ProviderStats shape)."""

    name: str
    ok: int = 0
    failed: int = 0
    last_latency_ms: float | None = None
    avg_latency_ms: float | None = None
    last_error: str | None = None
    last_success: str | None = None
    # Extra detail hook (old registry note).
    note: str | None = None


@dataclass(frozen=True)
class CacheHealth:
    """The lane cache split (old cache.py:107-113 shape)."""

    entries: int = 0
    stale_entries: int = 0
    disk_dir: str | None = None


@dataclass(frozen=True)
class FeedHealthFrame:
    """One FEEDS read: provider rows (name-sorted) + the cache line."""

    providers: tuple[ProviderHealth, ...] = ()
    cache: CacheHealth = CacheHealth()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def tracked(name: str, fn: Callable[[], Any]) -> Any:
    """Delegate to the shared registry's tracked call (one counter math)."""
    return _shared_registry.tracked(name, fn)


def all_stats() -> list[ProviderHealth]:
    """Shared counters, frozen and name-sorted (the FEEDS render order)."""
    return sorted(
        (ProviderHealth(**vars(row)) for row in _shared_registry.all_stats()),
        key=lambda row: row.name,
    )


def reset_stats() -> None:
    """Clear the shared counters (tests call this for determinism)."""
    _shared_registry.reset_stats()


def note_cache(entries: int, stale_entries: int,
               disk_dir: str | None = None) -> None:
    """Report the lane cache split (called by the feeds collector)."""
    with _LOCK:
        _CACHE.update({"entries": int(entries),
                       "stale_entries": int(stale_entries),
                       "disk_dir": disk_dir})


def cache_stats() -> CacheHealth:
    with _LOCK:
        return CacheHealth(**dict(_CACHE))


def latency_band(avg_latency_ms: float | None) -> str | None:
    """``ok``/``warn``/``crit`` for one EWMA average (None when unknown)."""
    if avg_latency_ms is None:
        return None
    if avg_latency_ms > LATENCY_CRIT_MS:
        return "crit"
    if avg_latency_ms > LATENCY_WARN_MS:
        return "warn"
    return "ok"


#: Stats source seam (defaults to this module's registry; tests inject).
StatsSource = Callable[[], "list[ProviderHealth]"]
CacheSource = Callable[[], CacheHealth]


class FeedHealthService:
    """Compose one FEEDS frame from injectable stats/cache readers."""

    def __init__(self, stats: StatsSource | None = None,
                 cache: CacheSource | None = None) -> None:
        self._stats = stats
        self._cache = cache

    def frame(self) -> FeedHealthFrame:
        """One health read; degrades to an error frame, never raises."""
        try:
            providers = tuple(self._stats() if self._stats is not None
                              else all_stats())
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            return FeedHealthFrame(error=f"stats error: {exc!r}")
        try:
            cache = (self._cache() if self._cache is not None
                     else cache_stats())
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            return FeedHealthFrame(providers=providers,
                                   error=f"cache error: {exc!r}")
        return FeedHealthFrame(providers=providers, cache=cache)


def health_summary(frame: FeedHealthFrame) -> str:
    """One-line status summary for the screen's cache strip (pure)."""
    cache = frame.cache
    fresh = f"{cache.entries} fresh"
    stale = f"{cache.stale_entries} stale"
    disk = f" | disk snapshots: {cache.disk_dir}" if cache.disk_dir else ""
    return f"cache: {fresh} / {stale}{disk}"


def provider_row(row: ProviderHealth) -> Mapping[str, object]:
    """One provider's display row (the old FEEDS table cells, pure)."""
    return {
        "name": row.name,
        "ok": str(row.ok),
        "failed": str(row.failed),
        "avg_ms": ("—" if row.avg_latency_ms is None
                   else f"{row.avg_latency_ms:.0f}"),
        "last_success": (row.last_success or "—").replace("T", " "),
        "last_error": row.last_error or "—",
    }
