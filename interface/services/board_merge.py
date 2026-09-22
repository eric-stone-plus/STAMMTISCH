"""Board row-merge protocol — the futures 3-leg merge, out of the screen.

Extracted (copy + adapt, no ``tui.`` import) from FuturesScreen's
``_apply_adapters`` / ``_apply_quotes`` (tui/screens/domains.py:537-589)
— the inventory called it "the most intricate data-merge code in the UI
and a prime extraction candidate".

THE PROTOCOL (contract; M5 board screens and any multi-leg collector
rely on every clause):

1. Legs land in ARBITRARY ORDER (fast CCI parquet board, slow SGX
   exchange-settlement adapter, streaming per-symbol quotes). No leg
   knows whether it is first.
2. A LATE LEG NEVER WIPES STANDING ROWS. A row that already carries
   merged data — live numbers or a concrete error — keeps that data;
   the late leg only supplies/refreshes the scaffold (static fields:
   source, unit, curve). Old domains.py:538-544: "quotes already merged
   win, symbols still loading keep their pending placeholder".
3. PENDING SURVIVES. A symbol with no served data yet keeps a pending
   placeholder row, so a fast leg landing first cannot delete the
   roster and a slow leg cannot blank the board mid-load.
4. THE QUOTE PASS UPDATES, NEVER REPLACES. A good quote overlays only
   the fields it carries onto the standing row, clears any error, and
   restores scaffold defaults the placeholder branch never had (old
   domains.py:577-586: ``item.pop("error")`` + ``setdefault("unit")``);
   an error quote sets the error and overlays nothing.

Adaptations from the screen methods:

- Rows are frozen ``BoardRow`` dataclasses instead of mutated dicts;
  every merge returns fresh containers (the collector-discipline rule
  for shallow-frozen contracts).
- The screen's two dict caches (``_quotes_cache`` merged data +
  rebuilt board) collapse into one standing-row mapping — the typed
  merger makes the "merged data wins" overlay explicit instead of
  implicit through a cache.
- ``recent``/``curve`` keep the old per-source row shapes as opaque
  mappings (sgx settle/OI/month rows vs OHLCV rows differ by source;
  the M5 detail panes re-shape columns per source, domains.py:501-535).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "BoardRow",
    "apply_quote",
    "is_merged",
    "is_pending",
    "merge_leg",
    "placeholder",
]

#: Placeholder error text — old domains.py:550's ``{"error": "pending"}``.
PENDING = "pending"

#: Quote-pass overlay fields (old domains.py:577).
_QUOTE_FIELDS = ("last", "chg_pct", "pct5", "pct20", "volume", "recent")


@dataclass(frozen=True)
class BoardRow:
    """One merged board row, typed over the merge-relevant fields."""

    key: str  # row key: provider code / instrument id
    source: str  # "yahoo" | "sgx" | "cci" | …
    last: float | None = None
    chg_pct: float | None = None
    pct5: float | None = None
    pct20: float | None = None
    volume: float | None = None
    error: str | None = None
    #: Scaffold fields the placeholder branch never carried (protocol 4).
    unit: str = ""
    curve: tuple[Mapping[str, Any], ...] = ()
    recent: tuple[Mapping[str, Any], ...] = ()
    name: str = ""


def placeholder(key: str, source: str = "yahoo") -> BoardRow:
    """The standing placeholder for a symbol still awaiting its legs."""
    return BoardRow(key=key, source=source, error=PENDING)


def is_pending(row: BoardRow) -> bool:
    """True while the row carries nothing but the pending placeholder."""
    return row.error == PENDING


def is_merged(row: BoardRow) -> bool:
    """True once any leg served the row: live data or a concrete error.

    Protocol 2/3 hinge on this: merged rows survive late legs; pending
    rows yield to whichever leg lands first. Mirrors the old cache
    semantics — ANY served field (or a concrete error) marks the row
    merged, however thin.
    """
    if is_pending(row):
        return False
    return (row.last is not None or row.chg_pct is not None
            or row.pct5 is not None or row.pct20 is not None
            or row.volume is not None or row.error is not None
            or bool(row.recent) or bool(row.curve))


def apply_quote(row: BoardRow, quote: Mapping[str, Any] | None) -> BoardRow:
    """Overlay one streamed quote onto one standing row (protocol 4).

    A falsy quote leaves the row untouched (old domains.py:569-571). An
    error quote sets the error — only when it CHANGED, so repaint
    suppression by equality still works — and overlays no numbers. A
    good quote overlays each carried field, clears the error, and
    restores the scaffold defaults a placeholder never had.
    """
    if not quote:
        return row
    if "error" in quote:
        error = str(quote["error"])
        if row.error == error:
            return row
        return replace(row, error=error)
    updates: dict[str, Any] = {"error": None}
    for field in _QUOTE_FIELDS:
        if field in quote:
            value = quote[field]
            if field in ("recent", "curve"):
                value = tuple(value)  # keep the row model tuple-typed
            updates[field] = value
    # Scaffold restore (old domains.py:584-585): placeholder rows were
    # built without the static board fields.
    updates.setdefault("unit", row.unit or "")
    updates.setdefault("curve", row.curve or ())
    return replace(row, **updates)


def merge_leg(
    standing: Mapping[str, BoardRow],
    leg: Mapping[str, BoardRow],
    roster: Iterable[str] = (),
) -> dict[str, BoardRow]:
    """Merge one adapter/settlement leg into the standing board.

    Protocol 2/3 realized: the leg's rows become the scaffold (fresh
    static fields, sources, full row sets); for keys where the standing
    board already holds MERGED data, that data wins and only the
    scaffold is refreshed from the leg; every roster symbol that no leg
    has served yet keeps (or gains) a pending placeholder.
    """
    merged: dict[str, BoardRow] = {}
    for key, leg_row in leg.items():
        prior = standing.get(key)
        if prior is not None and is_merged(prior):
            # Late legs never wipe standing rows: merged live fields
            # win; the leg refreshes only the scaffold.
            merged[key] = replace(
                prior, source=leg_row.source,
                unit=leg_row.unit or prior.unit,
                curve=leg_row.curve or prior.curve,
                name=leg_row.name or prior.name,
            )
        else:
            merged[key] = leg_row
    for key in roster:
        if key in merged:
            continue
        prior = standing.get(key)
        merged[key] = prior if (prior is not None and is_merged(prior)) \
            else placeholder(key)
    return merged
