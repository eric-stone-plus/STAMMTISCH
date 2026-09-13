"""TTL cache with stale-while-revalidate and disk last-known snapshots.

A fresh hit answers immediately. On expiry the producer runs again; a
producer failure falls back to the in-memory stale value, then to the
last disk snapshot (bounded by ``disk_ttl_seconds``), and only then
raises — so a total provider outage degrades to stale rows instead of
blank boards. Stale rows keep the ``source`` stamp of the provider that
produced them, which keeps the fail-closed provenance rule intact.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")

_MAX_STALE_ENTRIES = 512

_TTL: dict[str, tuple[float, Any]] = {}
_STALE: OrderedDict[str, Any] = OrderedDict()
_DISK_DIR: Path | None = None
_LOCK = threading.Lock()


def configure_disk_cache(directory: str | Path | None) -> None:
    """Point disk snapshots at a directory (typically <state_root>/intel/feedcache)."""
    global _DISK_DIR
    with _LOCK:
        _DISK_DIR = Path(directory) if directory else None


def _disk_path(key: str) -> Path | None:
    if _DISK_DIR is None:
        return None
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return _DISK_DIR / f"{digest}.json"


def _disk_read(key: str, max_age_seconds: float) -> Any:
    path = _disk_path(key)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if time.time() - float(payload.get("ts", 0)) > max_age_seconds:
        return None
    return payload.get("value")


def _disk_write(key: str, value: Any) -> None:
    path = _disk_path(key)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "key": key,
                                   "value": value}, default=str),
                       encoding="utf-8")
        tmp.replace(path)
    except (OSError, TypeError, ValueError):
        pass  # snapshots are best-effort; memory stale still covers outages


def cached(key: str, ttl_seconds: float, producer: Callable[[], T], *,
           disk_ttl_seconds: float = 3 * 86400) -> T:
    """Return a cached value for key, refreshing via producer() when stale."""
    now = time.time()
    with _LOCK:
        entry = _TTL.get(key)
        if entry is not None and now < entry[0]:
            return entry[1]
    try:
        value = producer()
    except Exception:
        with _LOCK:
            stale = _STALE.get(key)
        if stale is not None:
            return stale
        disk = _disk_read(key, disk_ttl_seconds)
        if disk is not None:
            return disk
        raise
    with _LOCK:
        _TTL[key] = (now + ttl_seconds, value)
        _STALE[key] = value
        _STALE.move_to_end(key)
        while len(_STALE) > _MAX_STALE_ENTRIES:
            _STALE.popitem(last=False)
    _disk_write(key, value)
    return value


def cache_stats() -> dict[str, Any]:
    with _LOCK:
        return {
            "entries": len(_TTL),
            "stale_entries": len(_STALE),
            "disk_dir": str(_DISK_DIR) if _DISK_DIR else None,
        }


def reset_cache() -> None:
    with _LOCK:
        _TTL.clear()
        _STALE.clear()
