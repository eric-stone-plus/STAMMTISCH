"""Shared CLI argument plumbing for the three interface tiers.

One shape everywhere (adversarial review A: three hand-rolled parsers had
drifted — one defaulted ``--demo`` to True with no off-switch, silently
ignoring ``--root``): explicit ``--demo`` (default OFF), ``--root PATH``
resolved through :func:`resolve_state_root` (explicit > ``STAMMTISCH_HOME``
> the conventional default when it exists). No tier ever falls back to
synthetic data silently.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

_DEFAULT_ROOT = Path.home() / ".local" / "share" / "stammtisch"


def add_data_args(parser: argparse.ArgumentParser) -> None:
    """The shared --demo/--root pair (explicit; no silent defaults)."""
    parser.add_argument("--demo", action="store_true",
                        help="synthetic deterministic data; no core, state "
                             "root or network needed")
    parser.add_argument("--root", type=Path, default=None, metavar="PATH",
                        help="STAMMTISCH state root (default: "
                             "$STAMMTISCH_HOME or ~/.local/share/stammtisch "
                             "when it exists)")


def resolve_state_root(explicit: Path | None) -> Path | None:
    """Resolve the state root: explicit flag > env > conventional default.

    Edge policy (grilling S1): an explicit relative ``--root`` resolves
    to an absolute path so the session key and every scan agree
    regardless of cwd drift; ``$STAMMTISCH_HOME`` pointing at a FILE is
    returned as-is — resolution itself must never crash, and the events
    collector degrades loudly ("cannot scan") on the unusable root
    instead of guessing a different one.
    """
    if explicit is not None:
        return Path(explicit).resolve()
    if env := os.environ.get("STAMMTISCH_HOME"):
        return Path(env)
    if _DEFAULT_ROOT.is_dir():
        return _DEFAULT_ROOT
    return None
