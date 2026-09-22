"""Letter flags — fixed-width, grep-friendly, colorblind-safe state cells.

Every state that colors also gets a letter (snapshot.FLAG_LETTERS order);
inactive flags render as a dim placeholder so the cell width never changes
and ``grep ' R '`` on a captured log finds running rows regardless of
color support. Mirrors the reviewed MOTOKO flag grammar with this
workstation's semantics.
"""

from __future__ import annotations

from collections.abc import Mapping

from interface.render.tokens import token
from interface.snapshot import FLAG_LETTERS


def letter_flags(active: Mapping[str, bool], *, styled: bool = True) -> str:
    """Render the six-flag cell: active letters, dim dots for the rest.

    ``styled=False`` returns the plain string (the one-shot status tier
    renders uncolored, pipe-safe output with the identical shape so rows
    correlate across tiers by text).
    """
    parts: list[str] = []
    for letter in FLAG_LETTERS:
        if active.get(letter):
            parts.append(letter)
        else:
            parts.append(".")
    plain = "".join(parts)
    if not styled:
        return plain
    from rich.text import Text

    cell = Text()
    for letter, char in zip(FLAG_LETTERS, plain):
        if char == letter:
            cell.append(char, style=f"bold {token(f'flag.{letter}')}")
        else:
            cell.append(char, style=token("text.muted"))
    return cell


def flag_cell_plain(active: Mapping[str, bool]) -> str:
    """Plain-text twin of :func:`letter_flags` for stdlib-only tiers."""
    return letter_flags(active, styled=False)
