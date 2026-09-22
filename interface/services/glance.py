"""GlanceService — the market-glance composition, out of the screen.

Extracted (copy + adapt, no ``tui.`` import) from the old TUI's
``DashboardScreen._glance_tick`` closure nest, tui/screens/dashboard.py:
155-234: one batch quote fetch over the glance index set, the CCI
realtime snapshot, the sentiment tape stances, and the provenance
``src:`` line naming the feeds that actually served this frame.

Adaptations from the screen closure:

- The three fetch legs (livefeed batch / ccifeed snapshot / sentiment
  stance) become injectable callables with no-op-ish defaults, so the
  service is pure from the caller's viewpoint and tests run offline.
  Every leg degrades silently, exactly like the old ``try/except`` passes.
- Output is a frozen ``GlanceFrame`` (rows, not formatted strings); the
  sparkline history bookkeeping (48-point cap, first series with >= 2
  points becomes the trend) moves here as the pure ``update_history``.
- The provenance line is honest per the old comment's intent ("name the
  sources that served this glance"): ``ccidx`` appears only when the CCI
  leg served and ``fin-daily`` only when a stance leg served. The old
  code appended both unconditionally (dashboard.py:227) — a small
  drift this copy fixes.
- A stance with a non-numeric score renders as 0.0 instead of raising
  inside the row loop (the old f-string ``{None:+.2f}`` would have
  blown the whole deliver callback).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "GLANCE_LABELS",
    "GLANCE_MARKETS",
    "GlanceCci",
    "GlanceFrame",
    "GlanceIndex",
    "GlanceService",
    "GlanceStance",
    "update_history",
]

#: The glance index set in display order — old dashboard.py:163,187.
GLANCE_LABELS: Mapping[str, str] = {
    "000001.SS": "SH COMP",
    "HSI": "HSI",
    "QQQ": "QQX(US)",
}

#: Sentiment tape markets probed per glance tick — old dashboard.py:174.
GLANCE_MARKETS: tuple[str, ...] = ("hk", "us")

#: Sparkline history cap per symbol — old dashboard.py:202 (``del series[:-48]``).
GLANCE_HISTORY_CAP = 48

#: A livefeed-style quote row: {last, prev_close, source, ...}.
QuoteRow = Mapping[str, Any]

#: Callable fetching a batch of quote rows ({symbol: row}); absent on failure.
FetchQuotes = Callable[[Sequence[str]], Mapping[str, QuoteRow]]

#: Callable returning the CCI realtime snapshot row ({last, chg_pct}), if any.
FetchCci = Callable[[], QuoteRow | None]

#: Callable returning one market's stance row ({stance, score, ...}), if any.
FetchStance = Callable[[str], Mapping[str, Any] | None]


def _empty_quotes(symbols: Sequence[str]) -> Mapping[str, QuoteRow]:
    """Default quote leg: nothing served (callers inject the real fetch)."""
    del symbols
    return {}


def _no_cci() -> QuoteRow | None:
    """Default CCI leg: the ccidx websocket is not wired into services yet."""
    return None


def _no_stance(market: str) -> Mapping[str, Any] | None:
    """Default stance leg: no reports root wired (M5 wires the tape)."""
    del market
    return None


@dataclass(frozen=True)
class GlanceIndex:
    """One glance index row (old dashboard.py:196-199)."""

    symbol: str
    label: str
    last: float
    chg_pct: float
    source: str


@dataclass(frozen=True)
class GlanceCci:
    """The CCI realtime overlay row (old dashboard.py:214-216)."""

    last: float
    chg_pct: float


@dataclass(frozen=True)
class GlanceStance:
    """One sentiment tape stance row (old dashboard.py:217-219)."""

    market: str
    stance: str
    score: float
    source: str = ""


@dataclass(frozen=True)
class GlanceFrame:
    """One glance tick's composition; every leg optional, none raising."""

    indices: tuple[GlanceIndex, ...] = ()
    cci: GlanceCci | None = None
    stances: tuple[GlanceStance, ...] = ()
    #: Provenance: the distinct serving sources, sorted, plus the CCI /
    #: sentiment tape labels when those legs served (old dashboard.py:222-227).
    sources: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when no leg served anything (old ``"(no data)"`` branch)."""
        return not (self.indices or self.cci or self.stances)


class GlanceService:
    """Compose one glance frame from injectable fetch legs; never raises."""

    def __init__(self, fetch_quotes: FetchQuotes = _empty_quotes,
                 fetch_cci: FetchCci = _no_cci,
                 fetch_stance: FetchStance = _no_stance) -> None:
        self._fetch_quotes = fetch_quotes
        self._fetch_cci = fetch_cci
        self._fetch_stance = fetch_stance

    def frame(self, labels: Mapping[str, str] = GLANCE_LABELS,
              markets: Sequence[str] = GLANCE_MARKETS) -> GlanceFrame:
        """Fetch every leg and compose the frame; a failed leg is absent."""
        quotes = self._quotes(labels)
        indices = []
        for symbol, label in labels.items():
            quote = quotes.get(symbol)
            if not quote:
                continue  # old dashboard.py:193-195 — absent symbols skip
            last = quote.get("last")
            if last is None:
                continue
            prev_close = quote.get("prev_close")
            chg = ((float(last) / float(prev_close) - 1.0) * 100.0
                   if prev_close else 0.0)
            indices.append(GlanceIndex(
                symbol=symbol, label=label, last=float(last),
                chg_pct=chg, source=str(quote.get("source", "")),
            ))
        cci = self._cci()
        stances = self._stances(markets)
        return GlanceFrame(
            indices=tuple(indices), cci=cci, stances=stances,
            sources=_provenance(indices, cci, stances),
        )

    # ── legs (each degrades like the old try/except passes) ────────────

    def _quotes(self, labels: Mapping[str, str]) -> Mapping[str, QuoteRow]:
        try:
            return self._fetch_quotes(list(labels)) or {}
        except Exception:  # noqa: BLE001 - leg absence, old pass-branch
            return {}

    def _cci(self) -> GlanceCci | None:
        try:
            snap = self._fetch_cci()
        except Exception:  # noqa: BLE001 - leg absence, old pass-branch
            return None
        if not snap or snap.get("last") is None:
            return None
        try:
            return GlanceCci(last=float(snap["last"]),
                             chg_pct=float(snap.get("chg_pct") or 0.0))
        except (TypeError, ValueError):
            return None

    def _stances(self, markets: Sequence[str]) -> tuple[GlanceStance, ...]:
        out: list[GlanceStance] = []
        for market in markets:
            try:
                stance = self._fetch_stance(market)
            except Exception:  # noqa: BLE001, S112 - leg absence, old pass
                continue
            if not stance:
                continue
            try:
                score = float(stance.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            out.append(GlanceStance(
                market=market, stance=str(stance.get("stance") or ""),
                score=score, source=str(stance.get("source") or ""),
            ))
        return tuple(out)


def _provenance(indices: Sequence[GlanceIndex], cci: GlanceCci | None,
                stances: Sequence[GlanceStance]) -> tuple[str, ...]:
    """The ``src:`` line assembly — first word of each serving source,
    sorted, plus the CCI / fin-daily labels only when those legs served
    (old dashboard.py:222-227, made honest to its own comment)."""
    sources = {
        row.source.split()[0] for row in indices if row.source.split()
    }
    if cci is not None:
        sources.add("ccidx")
    if stances:
        sources.add("fin-daily")
    return tuple(sorted(sources))


def update_history(
    history: Mapping[str, Sequence[float]], frame: GlanceFrame,
    cap: int = GLANCE_HISTORY_CAP,
) -> tuple[dict[str, list[float]], list[float]]:
    """Fold one frame into the sparkline history; pure, fresh containers.

    Replaces the screen-owned ``self._glance_history`` mutation (old
    dashboard.py:189-211): each served index appends its last price,
    each series keeps the newest ``cap`` points, and the trend is the
    first series (in label order) with at least two points.
    """
    updated = {sym: list(series) for sym, series in history.items()}
    for row in frame.indices:
        updated.setdefault(row.symbol, []).append(row.last)
    for series in updated.values():
        del series[:-cap]
    trend = next(
        (updated[sym] for sym in GLANCE_LABELS
         if len(updated.get(sym, ())) >= 2),
        [],
    )
    return updated, trend
