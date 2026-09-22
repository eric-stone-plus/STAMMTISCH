"""The spine staleness badge, shared by the spine-live screens.

D-M6 finding 4 (interface/REVIEWS/D-M6-standards.md:12): the badge
block was triplicated across the three spine-live screens
(feeds_health / energy / polymarket) — only the panel title differed.
STALE is data, not style (the spine's own rule): the spine hands each
screen a ``fresh``/``warn``/``crit`` level and this helper maps it to
the documented badge text, styled ONLY through
:mod:`interface.render.tokens` (no new hex — the ``stale.*`` tokens
already in the single color truth).

Rich-only, like all of ``render/``: usable from every tier, no Textual.
"""

from __future__ import annotations

from rich.text import Text

from interface.render.tokens import token

__all__ = ["stale_badged_title"]


def stale_badged_title(base: str, level: str) -> Text:
    """A panel title carrying the staleness badge for ``level``.

    ``fresh`` (and any unknown level — the spine's vocabulary is closed)
    renders the plain title; ``warn`` appends ``▲ stale?``; ``crit``
    appends a bold ``■ STALE``.
    """
    title = Text(base, style=f"bold {token('panel.title')}")
    if level == "warn":
        title.append("  \u25b2 stale?", style=token("stale.warn"))
    elif level == "crit":
        title.append("  \u25a0 STALE", style=f"bold {token('stale.crit')}")
    return title
