"""Semantic color tokens — the single color truth for the workstation.

Every hue rendered by any tier resolves through :data:`TOKENS`; no module
hardcodes a hex value (the old TUI carried three drifted layers of color
truth — theme CSS, widget constants, and ~20 inline per-screen strings).

Tokens are semantic, not positional: ``state.ok`` means "healthy/within
limits" wherever it renders. Market-direction colors (up/down) are product
decisions, not status colors: ``market.up``/``market.down`` carry the THS
convention (red up / green down) explicitly so nobody "fixes" them by
accident.

M0 ships the dark palette; light/colorblind variants arrive with the theme
loader in a later milestone. Downstream degradation (truecolor -> 256 ->
16) is left to rich/textual at render time — one source of truth per token,
never a hand-maintained 256-color map.
"""

from __future__ import annotations

from collections.abc import Mapping

TOKENS: Mapping[str, str] = {
    # surfaces
    "bg": "#000000",
    "panel.bg": "#0c0c0c",
    "panel.border": "#505050",
    "panel.title": "#ffffff",
    "text.primary": "#a0a0a0",
    "text.muted": "#606060",
    "accent": "#4fc3f7",
    # states
    "state.ok": "#66bb6a",
    "state.warn": "#ffd54f",
    "state.crit": "#ef5350",
    "state.info": "#4fc3f7",
    "state.unknown": "#606060",
    # market direction (THS convention: red up, green down — product rule)
    "market.up": "#ef5350",
    "market.down": "#66bb6a",
    # feed / events
    "feed.ts": "#606060",
    "feed.kind_run": "#4fc3f7",
    "feed.kind_stage": "#a0a0a0",
    "feed.kind_gate_pass": "#66bb6a",
    "feed.kind_gate_fail": "#ef5350",
    "feed.kind_intake": "#4fc3f7",
    # letter flags (flag.<letter> per snapshot.FLAG_LETTERS)
    "flag.R": "#4fc3f7",
    "flag.H": "#ef5350",
    "flag.G": "#ffd54f",
    "flag.F": "#ffd54f",
    "flag.D": "#ef5350",
    "flag.C": "#4fc3f7",
    # provenance / cost
    "provenance.src": "#606060",
    "cost.total": "#4fc3f7",
    "stale.warn": "#ffd54f",
    "stale.crit": "#ef5350",
}

#: Run-state -> token resolution (the status badge palette of the old TUI,
#: collapsed into the single truth).
STATE_TOKENS: Mapping[str, str] = {
    "completed": "state.ok",
    "accepted": "state.ok",
    "running": "state.info",
    "capturing": "state.info",
    "staged": "state.info",
    "created": "state.info",
    "resumed": "state.info",
    "reconciled": "state.info",
    "gating": "state.warn",
    "blocked": "state.warn",
    "interrupted": "state.warn",
    "rejected": "state.crit",
    "failed": "state.crit",
    "halted": "state.crit",
    "corrupt": "state.crit",
    "unknown": "state.unknown",
}


def token(name: str) -> str:
    """Resolve one semantic token to its hex value (KeyError on typos —
    a misspelled token must fail loudly, never render black-on-black)."""
    return TOKENS[name]
