"""Service lane — UI-free business logic behind the screens and collectors.

``interface/services`` is the M3/M4 addition to the boundary rule: pure
service adapters, each extracted (copy + adapt, no ``tui.`` import)
from the old TUI with the source file:line cited in its docstring —
the quant engine (M3), the glance composition, the decisions reader,
the board row-merge protocol, the intake result formatter, and the
livefeed quotes provider (M4); plus the M6 absorption wave (feeds
health counters, the trade-ledger FIFO fold, the EIA energy watchlist,
the Polymarket Gamma tape, the daily-report sentiment tape — each
behind injectable transports/paths), plus the shared pinned-proxy
egress contract the two HTTP twins delegate to (review D extraction).
Modules here may import the
snapshot contract; they must never import Textual or reach back into
the UI or collector layers (``tests/test_boundaries.py`` enforces all
of it).
"""
