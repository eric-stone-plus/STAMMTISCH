"""Free-data feed layer — provider modules, fallback chains, caching.

The pattern (borrowed from open-source market terminals such as
OpenTerminal/OpenBB, reimplemented stdlib-only):

- ``providers/`` — one small isolated module per public data source,
  each exposing a ``fetch_*`` call that returns the same normalized
  shapes as every other provider (a quote dict with a ``source`` stamp,
  or a candle dict). Swapping or adding a source touches one file.
- ``registry`` — ``with_fallback`` tries providers in order and records
  per-provider health (ok/failed, latency, last error) for the FEEDS
  panel.
- ``cache`` — TTL cache with stale-while-revalidate: when every provider
  for a key is down, the last-known value still renders, stamped with
  its original source.

Fail-closed discipline: a fallback result is never silent. Every row
carries its ``source`` stamp and boards render the stamps they were
fed, so a Yahoo-sourced row is always distinguishable from a Tencent
row. Absent symbols stay absent — a provider outage degrades to
per-cell no-data, never to fabricated rows.
"""

from __future__ import annotations

from .errors import FeedError
from .registry import ProviderStats, all_stats, reset_stats, tracked, with_fallback
from .cache import cache_stats, cached, configure_disk_cache, reset_cache
from . import service

__all__ = [
    "FeedError",
    "ProviderStats",
    "all_stats",
    "reset_stats",
    "tracked",
    "with_fallback",
    "cache_stats",
    "cached",
    "configure_disk_cache",
    "reset_cache",
    "service",
]
