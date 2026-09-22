"""Screen tier — Textual screens built on render/panels renderables.

- ``overview.py``      the Overview wall (runs table, activity feed,
                       glance, services strip) and the RunDetail modal.
- ``workbench.py``     the spec-driven five quant tools (M3).
- ``runs_detail.py``   the ``:detail <run-id>`` streaming run screen.
- M6 absorbed read-only screens (RETIREMENT Option B): ``feeds_health``,
  ``ledger``, ``energy``, ``polymarket``, ``sentiment`` — each renders a
  UI-free ``services/`` contract, reaches it by injection only, and
  (where live) polls it on the spine's slow services lane.

Screens hold layout and interaction only: cadence comes from the spine,
colors from ``render/tokens.py``, panel bodies from ``render/panels.py``.
Import ``interface.screens.overview`` only where Textual is wanted.
"""
