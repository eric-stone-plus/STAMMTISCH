"""Render layer — pure snapshot -> Rich renderables, plus shared tokens.

No Textual import anywhere in this package (the one-shot status tier must
run on bare python3; submodules load lazily so importing ``render.tokens``
never drags rich into the process).

Single color truth: every hue the workstation renders resolves through
``tokens.TOKENS`` (semantic names -> sRGB hex). Widget chrome maps the same
tokens onto Textual theme variables in the app layer; no module may
hardcode a hex value.
"""

from __future__ import annotations

__all__ = ["flags", "panels", "tokens"]

_SUBMODULES = ("flags", "panels", "tokens")


def __getattr__(name: str):
    if name in _SUBMODULES:
        import importlib

        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
