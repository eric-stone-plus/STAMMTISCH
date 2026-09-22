"""DecisionReader — the decisions/latest.json plane, out of the screen.

Extracted (copy + adapt, no ``tui.`` import) from the old TUI's domains
module: ``_decision_symbols`` and ``security_watchlist``
(tui/screens/domains.py:1144-1230) plus SecurityScreen's
``_load_decision`` (tui/screens/domains.py:1661-1691).

The decisions file is written by the standalone ``decide.py`` CLI (old
tree, blueprint KEEP list); this reader serves two consumers:

- the SECURITY board roster: non-cut acted symbols plus the screened
  ``scan_top`` candidates, merged with the manual watchlist and recents
  (old domains.py:1195-1230);
- the per-symbol decision detail (action + thesis card, old
  domains.py:1661-1691).

Adaptations:

- The old functions fished ``state_root`` out of a live config/driver
  object; the service takes the resolved root explicitly (the caller
  already knows it) with the old conventional default
  (``~/.local/share/stammtisch``) kept for parity.
- Symbol normalization reuses the compact resolver copy already shipped
  in :mod:`interface.services.quant_engine` (the old code imported
  ``services.engine._normalize_symbol``).
- ``_load_decision`` was less defensive than ``_decision_symbols`` (a
  non-dict position row raised, killing the whole read via the outer
  ``except``). This copy unifies on the defensive shape: malformed
  pieces are skipped, the file read degrades to empty — same effective
  behavior (bad data never reaches the board) without the whole-file
  loss.
- The scan-top thesis keeps the old Chinese card format verbatim
  (domains.py:1683-1687); it is decide.py's provenance vocabulary, not
  this package's chrome.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from interface.services.quant_engine import _normalize_symbol

__all__ = [
    "DECISION_VERSION",
    "DecisionPosition",
    "decision_positions",
    "decision_symbols",
    "security_watchlist",
]

#: The only decisions schema this reader answers to (old domains.py:1141).
DECISION_VERSION = 1

#: Old fallback root (domains.py:1150-1151, 1664-1665).
DEFAULT_STATE_ROOT = Path.home() / ".local/share/stammtisch"


@dataclass(frozen=True)
class DecisionPosition:
    """One symbol's decision row: acted position or screened candidate.

    ``action`` is the decide.py verb (``buy``/``hold``/``cut``/…) for
    acted positions, or ``#rank`` for ``scan_top`` candidates (old
    domains.py:1678-1688).
    """

    symbol: str
    action: str
    thesis: str


def _load_payload(state_root: Path | str | None) -> dict[str, Any] | None:
    """Read + shape-check decisions/latest.json; anything wrong → None."""
    try:
        base = Path(state_root).expanduser() if state_root else (
            DEFAULT_STATE_ROOT)
        payload = json.loads(
            (base / "decisions" / "latest.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - absent/unreadable/malformed → empty
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("decision_version") != DECISION_VERSION:
        return None
    return payload


def _zones(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    zones = payload.get("zones") or {}
    return zones if isinstance(zones, dict) else {}


def decision_positions(
    state_root: Path | str | None = None,
) -> dict[str, DecisionPosition]:
    """Latest decision positions keyed by UPPER symbol (old 1661-1691).

    Acted positions win over ``scan_top`` candidates for the same symbol
    (the old ``if symbol and symbol not in out`` guard). Malformed rows
    are skipped; a missing/unreadable/unsupported file yields ``{}``.
    """
    payload = _load_payload(state_root)
    if payload is None:
        return {}
    out: dict[str, DecisionPosition] = {}
    for zone_data in _zones(payload).values():
        if not isinstance(zone_data, dict):
            continue
        positions = zone_data.get("positions")
        if isinstance(positions, list):
            for position in positions:
                if not isinstance(position, dict):
                    continue
                symbol = str(position.get("symbol") or "").strip().upper()
                if not symbol:
                    continue
                out[symbol] = DecisionPosition(
                    symbol=symbol,
                    action=str(position.get("action") or "—"),
                    thesis=str(position.get("thesis")
                               or position.get("reason") or "")[:120],
                )
        # Screened candidates: action shows the screen rank (#1…); the
        # thesis carries the backtest card (old domains.py:1677-1688).
        scan_top = zone_data.get("scan_top")
        if isinstance(scan_top, list):
            for rank, row in enumerate(scan_top, 1):
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol") or "").strip().upper()
                if not symbol or symbol in out:
                    continue
                out[symbol] = DecisionPosition(
                    symbol=symbol,
                    action=f"#{rank}",
                    thesis=(f"scan #{rank} TR{row.get('tr')}% "
                            f"Sharpe{row.get('sharpe')} "
                            f"DD-{row.get('maxdd')}% "
                            f"win{row.get('win')}% "
                            f"{row.get('trades')}笔"),
                )
    return out


def decision_symbols(state_root: Path | str | None = None) -> list[str]:
    """Non-cut acted symbols + screened candidates, in file order.

    Old ``_decision_symbols`` (domains.py:1144-1184): ``cut`` positions
    are excluded (the board does not quote what it just cut);
    ``scan_top`` symbols are included unranked so the screened page
    fills the board — acted positions alone leave it half-empty.
    """
    payload = _load_payload(state_root)
    if payload is None:
        return []
    out: list[str] = []
    for zone_data in _zones(payload).values():
        if not isinstance(zone_data, dict):
            continue
        positions = zone_data.get("positions")
        if isinstance(positions, list):
            for position in positions:
                if not isinstance(position, dict):
                    continue
                if str(position.get("action") or "").strip().lower() == "cut":
                    continue
                symbol = str(position.get("symbol") or "").strip()
                if symbol:
                    out.append(symbol)
        scan_top = zone_data.get("scan_top")
        if isinstance(scan_top, list):
            for row in scan_top:
                if isinstance(row, dict):
                    symbol = str(row.get("symbol") or "").strip()
                    if symbol:
                        out.append(symbol)
    return out


def security_watchlist(
    manual: Sequence[str] | None,
    recents: Sequence[str] | None = None,
    state_root: Path | str | None = None,
) -> list[str]:
    """Board names: manual watchlist anchored, decision picks + recents merged.

    Old ``security_watchlist`` (domains.py:1195-1230): a non-empty manual
    list anchors the board (persistent cross-market core names) while
    today's decision picks and recents still flow in; an empty manual
    list is a decide.py full-market-screen signal, NOT an empty board —
    decisions and recents merge regardless. Every symbol is normalized
    (exchange suffixes resolved) and de-duplicated in first-seen order.
    """
    manual_syms = [str(s).strip() for s in (manual or []) if str(s).strip()]
    recent_syms = [str(s) for s in (recents or [])]
    if manual_syms:
        source: Iterable[str] = (manual_syms + decision_symbols(state_root)
                                 + recent_syms)
    else:
        source = decision_symbols(state_root) + recent_syms
    seen: list[str] = []
    for raw in source:
        text = str(raw).strip()
        if not text:
            continue
        symbol = _normalize_symbol(text.upper())
        if symbol not in seen:
            seen.append(symbol)
    return seen
