"""Provider registry — ordered fallback chains + per-provider health.

Each data source is wrapped in ``tracked`` so every call records ok /
failed, latency (EWMA), and the last error. The FEEDS panel renders
``all_stats()``; tests call ``reset_stats()``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from .errors import FeedError

T = TypeVar("T")

_STATS: dict[str, "ProviderStats"] = {}
_LOCK = threading.Lock()


@dataclass
class ProviderStats:
    """Rolling health for one named provider."""

    name: str
    ok: int = 0
    failed: int = 0
    last_latency_ms: float | None = None
    avg_latency_ms: float | None = None
    last_error: str | None = None
    last_success: str | None = None
    # Extra detail hooks (e.g. which symbols a batch provider missed).
    note: str | None = None


def _stats(name: str) -> ProviderStats:
    with _LOCK:
        entry = _STATS.get(name)
        if entry is None:
            entry = _STATS[name] = ProviderStats(name=name)
        return entry


def tracked(name: str, fn: Callable[[], T]) -> T:
    """Run fn while recording latency/health for the named provider."""
    entry = _stats(name)
    start = datetime.now(timezone.utc)
    try:
        result = fn()
    except Exception as exc:
        with _LOCK:
            entry.failed += 1
            entry.last_error = str(exc)[:200]
        raise
    elapsed_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000.0
    with _LOCK:
        entry.ok += 1
        entry.last_latency_ms = elapsed_ms
        entry.avg_latency_ms = (
            elapsed_ms if entry.avg_latency_ms is None
            else round(entry.avg_latency_ms * 0.8 + elapsed_ms * 0.2, 1)
        )
        entry.last_success = datetime.now(timezone.utc).isoformat(
            timespec="seconds")
        entry.last_error = None
    return result


def with_fallback(
    attempts: list[tuple[str, Callable[[], T]]],
    *,
    on_fallback: Callable[[str, str], None] | None = None,
) -> T:
    """Try each (provider, fn) in order; return the first success.

    All failures propagate as the last exception once the chain is
    exhausted — the caller decides whether that means blank cells or a
    visible error. ``on_fallback(provider, error)`` fires for every
    skipped provider so callers can surface the degradation instead of
    swallowing it.
    """
    last_error: Exception | None = None
    for name, fn in attempts:
        try:
            return tracked(name, fn)
        except Exception as exc:
            last_error = exc
            if on_fallback is not None:
                message = exc.message if isinstance(exc, FeedError) else str(exc)
                on_fallback(name, str(message)[:200])
    if last_error is not None:
        raise last_error
    raise FeedError("registry", "no providers configured")


def all_stats() -> list[ProviderStats]:
    with _LOCK:
        return [ProviderStats(**vars(entry)) for entry in _STATS.values()]


def reset_stats() -> None:
    with _LOCK:
        _STATS.clear()
