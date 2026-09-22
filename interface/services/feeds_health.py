"""FeedsHealthService — provider counters + lane cache, out of the screen.

Copy + adapt (no ``tui.`` import) of the old FEEDS panel's data side:

- the per-provider health counters contract is copied from
  ``tui/datafeeds/registry.py:24-103`` (:class:`ProviderStats`,
  ``tracked``, ``all_stats``): every tracked call records ok/failed, the
  EWMA latency (``avg = avg*0.8 + last*0.2``, seeded by the first call)
  and the last error/success stamps. The old screen rendered
  ``all_stats()`` sorted by name — this module keeps that contract and
  this tree's feeds lane reports into it
  (:mod:`interface.services.feeds` wraps the provider calls).
- the cache stats shape is copied from ``tui/datafeeds/cache.py:107-113``
  (``{entries, stale_entries, disk_dir}``). The old cache was the
  datafeeds TTL+disk store (old-tree machinery, not copied); this tree's
  equivalent is the feeds collector's keep-last-good row set, which
  REPORTS its fresh/stale split here (``note_cache``) — the honest
  adaptation: the FEEDS screen answers "what is serving me right now",
  not "what is on the old tree's disk".

Latency bands (new, for the screen's marking): the EWMA average falls
into ok/warn/crit bands — thresholds shared by every consumer, like the
snapshot's other semantic thresholds.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar

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

T = TypeVar("T")

#: EWMA smoothing factors — old registry.py:61-63.
_EWMA_KEEP = 0.8
_EWMA_NEW = 0.2

#: Semantic latency bands for the FEEDS table (avg over the EWMA).
LATENCY_WARN_MS = 1_500.0
LATENCY_CRIT_MS = 4_000.0

_STATS: dict[str, ProviderHealth] = {}
_LOCK = threading.Lock()

#: Lane cache stats reported by the feeds collector (old cache_stats shape).
_CACHE: dict[str, Any] = {"entries": 0, "stale_entries": 0, "disk_dir": None}


@dataclass(frozen=True)
class ProviderHealth:
    """Rolling health for one named provider (old ProviderStats shape)."""

    name: str
    ok: int = 0
    failed: int = 0
    last_latency_ms: float | None = None
    avg_latency_ms: float | None = None
    last_error: str | None = None
    last_success: str | None = None
    # Extra detail hook (old registry.py:34).
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


def _stats(name: str) -> ProviderHealth:
    with _LOCK:
        entry = _STATS.get(name)
        if entry is None:
            entry = _STATS[name] = ProviderHealth(name=name)
        return entry


def tracked(name: str, fn: Callable[[], T]) -> T:
    """Run ``fn`` while recording latency/health for the named provider.

    Copy of tui/datafeeds/registry.py:46-68: a raising call counts as a
    failure (last error kept, 200-char cap) and re-raises; a successful
    call bumps ok, the latencies and the success stamp, and clears the
    last error. Rows are frozen, so updates re-read the CURRENT row
    under the lock before replacing it (no lost increments).
    """
    entry = _stats(name)
    start = datetime.now(timezone.utc)
    try:
        result = fn()
    except Exception as exc:
        with _LOCK:
            current = _STATS.get(name, entry)
            entry = _STATS[name] = ProviderHealth(
                name=name, ok=current.ok, failed=current.failed + 1,
                last_latency_ms=current.last_latency_ms,
                avg_latency_ms=current.avg_latency_ms,
                last_error=str(exc)[:200],
                last_success=current.last_success, note=current.note)
        raise
    elapsed_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000.0
    with _LOCK:
        current = _STATS.get(name, entry)
        avg = (elapsed_ms if current.avg_latency_ms is None
               else round(current.avg_latency_ms * _EWMA_KEEP
                          + elapsed_ms * _EWMA_NEW, 1))
        entry = _STATS[name] = ProviderHealth(
            name=name, ok=current.ok + 1, failed=current.failed,
            last_latency_ms=elapsed_ms, avg_latency_ms=avg,
            last_error=None,
            last_success=datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            note=current.note)
    return result


def all_stats() -> list[ProviderHealth]:
    """Snapshot of every provider's counters (fresh rows, sorted by name)."""
    with _LOCK:
        rows = sorted(_STATS.values(), key=lambda row: row.name)
    return list(rows)


def reset_stats() -> None:
    """Clear the counters (tests call this for determinism)."""
    with _LOCK:
        _STATS.clear()


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
