"""App tier — the Textual workstation (M1: refresh spine + Overview wall).

- ``spine.py``   the refresh spine: global tick, per-panel cadence,
                 single-flight provider polls, staleness as data. Pure
                 core importable without Textual (tier 2 reuses it).
- ``shell.py``   the thin App: provider via ``collectors.build_provider``,
                 theme mapped from ``render/tokens.py``, screens mounted.

Import ``interface.app.spine`` freely; import ``interface.app.shell``
only where a Textual app is wanted (it pulls Textual in).
"""
