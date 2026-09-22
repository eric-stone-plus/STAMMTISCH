"""Shared service lane — the UI-free modules both front ends call.

M7 true merge (blueprint §2): the KEEP-listed service modules moved
here from ``tui/`` via ``git mv`` (history preserved), so the old
workstation (``tui/``) and the rebuilt interface (``interface/``)
consume ONE implementation instead of drifting copies. Everything in
this package is UI-free by construction and pinned statically by
``services/tests/test_boundaries.py``: no module under ``services/``
imports ``tui``, ``interface``, or Textual. Config, the quant engine,
the datafeeds stack, the intake validator/supervisor/steward, the
chart server, brokers, and the symbol resolvers live here; screens,
widgets, themes, and command palettes stay with their front ends.
"""
