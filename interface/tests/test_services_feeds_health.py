"""FeedsHealthService tests: the tracked()/all_stats() counters contract
copied from tui/datafeeds/registry.py:24-103 and the cache line shape
from tui/datafeeds/cache.py:107-113 — ok/fail counting, the EWMA
latency, last-error/success stamps, and the frame composition."""

from __future__ import annotations

from interface.services.feeds_health import (
    LATENCY_CRIT_MS,
    LATENCY_WARN_MS,
    CacheHealth,
    FeedHealthFrame,
    FeedHealthService,
    ProviderHealth,
    all_stats,
    cache_stats,
    health_summary,
    latency_band,
    note_cache,
    provider_row,
    reset_stats,
    tracked,
)


def setup_function(_) -> None:
    reset_stats()


def test_tracked_records_ok_latency_and_success() -> None:
    result = tracked("tencent", lambda: 42)
    assert result == 42, "tracked returns fn()'s result verbatim"
    (row,) = all_stats()
    assert row.name == "tencent"
    assert row.ok == 1 and row.failed == 0
    assert row.avg_latency_ms is not None, "first call seeds the EWMA"
    assert row.last_error is None
    assert row.last_success and "T" in row.last_success


def test_tracked_ewma_blends_like_the_old_registry() -> None:
    """avg = avg*0.8 + last*0.2 after the seeding call (registry.py:61-63)."""
    tracked("p", lambda: None)  # seed with an unmeurable-fast call
    (first,) = all_stats()
    tracked("p", lambda: None)
    (second,) = all_stats()
    assert second.avg_latency_ms == round(
        first.avg_latency_ms * 0.8 + second.last_latency_ms * 0.2, 1)


def test_tracked_failure_counts_and_reraises() -> None:
    def boom():
        raise RuntimeError("connection reset by peer " + "x" * 300)

    try:
        tracked("yahoo", boom)
    except RuntimeError:
        pass
    else:  # pragma: no cover - must re-raise
        raise AssertionError("tracked must re-raise the provider error")
    (row,) = all_stats()
    assert row.ok == 0 and row.failed == 1
    assert row.last_error is not None
    assert len(row.last_error) <= 200, "old 200-char cap kept"
    assert row.last_success is None


def test_success_clears_a_previous_error() -> None:
    state = {"bad": True}

    def flaky():
        if state["bad"]:
            state["bad"] = False
            raise RuntimeError("transient")
        return "ok"

    try:
        tracked("p", flaky)
    except RuntimeError:
        pass
    tracked("p", flaky)
    (row,) = all_stats()
    assert row.ok == 1 and row.failed == 1 and row.last_error is None


def test_all_stats_sorted_by_name_with_fresh_rows() -> None:
    tracked("zeta", lambda: None)
    tracked("alpha", lambda: None)
    rows = all_stats()
    assert [r.name for r in rows] == ["alpha", "zeta"]
    assert all(r.ok == 1 for r in rows), "fresh rows carry the counters"


def test_cache_stats_shape_and_reporting() -> None:
    assert cache_stats() == CacheHealth(entries=0, stale_entries=0,
                                        disk_dir=None)
    note_cache(3, 1)
    assert cache_stats() == CacheHealth(entries=3, stale_entries=1,
                                        disk_dir=None)
    note_cache(2, 0, disk_dir="/tmp/feedcache")
    assert cache_stats().disk_dir == "/tmp/feedcache"


def test_health_summary_renders_the_old_cache_line() -> None:
    frame = FeedHealthFrame(
        cache=CacheHealth(entries=4, stale_entries=2, disk_dir="/d"))
    assert health_summary(frame) == (
        "cache: 4 fresh / 2 stale | disk snapshots: /d")
    frame = FeedHealthFrame(cache=CacheHealth(entries=1, stale_entries=0))
    assert health_summary(frame) == "cache: 1 fresh / 0 stale"


def test_provider_row_matches_the_old_feeds_table_cells() -> None:
    row = ProviderHealth(name="tencent", ok=3, failed=1,
                         avg_latency_ms=234.56, last_success="2026-09-22T08:00:00",
                         last_error=None)
    cells = provider_row(row)
    assert cells["name"] == "tencent"
    assert cells["ok"] == "3" and cells["failed"] == "1"
    assert cells["avg_ms"] == "235"
    assert cells["last_success"] == "2026-09-22 08:00:00"  # T stripped
    assert cells["last_error"] == "—"
    blank = provider_row(ProviderHealth(name="x"))
    assert blank["avg_ms"] == "—" and blank["last_success"] == "—"


def test_latency_bands() -> None:
    assert latency_band(None) is None
    assert latency_band(10.0) == "ok"
    assert latency_band(LATENCY_WARN_MS + 1) == "warn"
    assert latency_band(LATENCY_CRIT_MS + 1) == "crit"


def test_service_frame_composes_stats_and_cache() -> None:
    tracked("tencent", lambda: None)
    note_cache(2, 1)
    frame = FeedHealthService().frame()
    assert frame.ok and frame.error is None
    assert [r.name for r in frame.providers] == ["tencent"]
    assert frame.cache.entries == 2 and frame.cache.stale_entries == 1


def test_service_seams_are_injectable_and_never_raise() -> None:
    def boom():
        raise RuntimeError("registry exploded")

    frame = FeedHealthService(stats=boom).frame()
    assert not frame.ok and "stats error" in frame.error

    frame = FeedHealthService(cache=boom).frame()
    assert not frame.ok and "cache error" in frame.error

    stub = FeedHealthService(
        stats=lambda: [ProviderHealth(name="stub", ok=9)],
        cache=lambda: CacheHealth(entries=5, stale_entries=0))
    served = stub.frame()
    assert served.providers[0].ok == 9 and served.cache.entries == 5
