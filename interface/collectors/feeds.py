"""FeedsCollector — the optional quotes plane behind the snapshot.

M4 addition closing the M2 gap (``WorkstationSnapshot.quotes`` was
demo-only): a collector-side provider over the services copy of the old
livefeed contract (:mod:`interface.services.feeds`, copied from
services/livefeed.py). The demo path never touches this module; the real
path (``--root``) assembles quotes with per-symbol age, the serving
source, and STALE semantics:

- rows carry honest ``age_s`` (provider timestamps normalized), so the
  contract's ``QUOTE_STALE_S`` / F-flag logic lights up on its own;
- a failed refresh KEEPS the last good rows (aged, going stale) — the
  old boards' degrade rule ("a dead feed never crashes the board; the
  UI keeps the last values") — and retries after the TTL;
- the fetch is injectable and resolved at call time, so tests run
  offline; a first-fetch failure or an empty set renders as NO quotes
  (never fabricated rows).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from interface.services.feeds import quote_age_s
from interface.services.feeds_health import note_cache
from interface.snapshot import QUOTE_STALE_S, QuoteSnapshot

__all__ = ["FeedsCollector"]

#: One refresh per window; 30s is the old dashboard glance cadence
#: (tui/screens/dashboard.py:291).
DEFAULT_TTL_S = 30.0

FetchBatch = Callable[[list[str]], Mapping[str, Mapping[str, Any]]]


def _real_fetch(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Default leg: the services copy of the livefeed contract.

    Resolved at call time (not import time) so tests can stub the
    module attribute and stay offline.
    """
    from interface.services.feeds import fetch_batch

    return fetch_batch(symbols)


class FeedsCollector:
    """TTL-batched quote rows → ``QuoteSnapshot`` frames; never raises."""

    def __init__(self, symbols: Sequence[str],
                 labels: Mapping[str, str] | None = None,
                 fetch: FetchBatch | None = None,
                 ttl_s: float = DEFAULT_TTL_S) -> None:
        self.symbols = tuple(symbols)
        self.labels = dict(labels) if labels else {}
        self._fetch = fetch
        self._ttl = ttl_s
        self._rows: dict[str, dict[str, Any]] = {}
        self._fetched_at: float | None = None
        self.fetch_calls = 0  # test accounting (cadence proof)

    def _refresh(self, now: float) -> None:
        self.fetch_calls += 1
        self._fetched_at = now
        fetch = self._fetch or _real_fetch
        try:
            rows = fetch(list(self.symbols)) or {}
        except Exception:  # noqa: BLE001 - keep last good, age it
            return
        if rows:
            # A served set replaces the cache; an empty/failed one does
            # not — stale-but-real beats absent.
            self._rows = dict(rows)

    def collect(self, now: float | None = None) -> tuple[QuoteSnapshot, ...]:
        """One quotes lane turn; fresh containers, never raises.

        M6: each turn also reports the fresh/stale split of the
        keep-last-good set into the feeds-health lane (the old FEEDS
        screen's cache line) — reporting only, never a behavior change.
        """
        current = time.time() if now is None else now
        if self._fetched_at is None or current - self._fetched_at >= self._ttl:
            self._refresh(current)
        rows = tuple(self._row(sym, row, current)
                     for sym, row in sorted(self._rows.items()))
        fresh = sum(1 for row in rows if row.age_s < QUOTE_STALE_S)
        note_cache(fresh, len(rows) - fresh)
        return rows

    def _row(self, symbol: str, quote: Mapping[str, Any],
             now: float) -> QuoteSnapshot:
        last = quote.get("last")
        prev_close = quote.get("prev_close")
        change = ((float(last) / float(prev_close) - 1.0) * 100.0
                  if (last is not None and prev_close) else 0.0)
        age = quote_age_s(dict(quote), now)
        if age is None:
            # Undatable stamp: fall back to when WE fetched it, so age
            # still grows toward STALE instead of freezing at zero.
            age = 0.0 if self._fetched_at is None else max(
                0.0, now - self._fetched_at)
        return QuoteSnapshot(
            symbol=symbol,
            label=self.labels.get(symbol, symbol),
            last=float(last) if last is not None else 0.0,
            change_pct=change,
            source=str(quote.get("source", "")),
            age_s=age,
        )
