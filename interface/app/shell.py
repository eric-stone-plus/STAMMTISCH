"""The workstation shell — a thin Textual App that owns nothing but wiring.

``WorkstationShell`` constructs the provider through
:func:`interface.collectors.build_provider`, hands it to the refresh
spine, registers the route table with the :class:`~interface.app.router.Router`
(palette ⌃P and the ``:`` command bar are its two entry points), and maps
the single color truth (:mod:`interface.render.tokens`) onto Textual
theme variables — no hex literal exists outside ``tokens.py``. All
cadence, staleness and error-episode logic lives in the spine; all
rendering lives in ``render/panels.py``; this module is plumbing only.

The ONE write path (M5): ``:delete <run-id>`` routes to the fail-closed
:class:`~interface.app.confirm.ConfirmDialog`; a confirmed verdict runs
the core-CLI delete OFF the UI thread behind its own
:class:`~interface.app.spine.SingleFlight` slot (never overlapping the
spine's polls or another delete), lands as notify + activity-feed audit
lines (``ui.delete confirmed/cancelled run=<id>`` plus the core's own
verdict), and forces one spine refresh so the wall reflects the removal.

``python -m interface.app.shell [--demo|--root PATH]`` starts the app.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.app import App
from textual.binding import Binding
from textual.theme import Theme

from interface.app.confirm import ConfirmDialog
from interface.app.help_overlay import HelpOverlay, help_rows
from interface.app.router import (
    ROOT_ROUTE,
    CommandBarScreen,
    CommandPaletteScreen,
    Router,
)
from interface.app.spine import RefreshSpine, SingleFlight
from interface.args import add_data_args, resolve_state_root
from interface.collectors import build_provider, session_for
from interface.render.tokens import token
from interface.screens.energy import EnergyScreen
from interface.screens.feeds_health import FeedsHealthScreen
from interface.screens.ledger import LedgerScreen
from interface.screens.overview import OverviewScreen
from interface.screens.polymarket import PolymarketScreen
from interface.screens.runs_detail import RunsDetailScreen
from interface.screens.sentiment import SentimentScreen
from interface.screens.workbench import WorkbenchScreen
from interface.snapshot import SnapshotProvider, WorkstationSnapshot

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from interface.collectors.core_cli import CliEnvelope

__all__ = ["WorkstationShell", "main"]

#: Semantic tokens -> Textual CSS variables (``$variable`` in CSS). Every
#: variable the stylesheet references is defined here, and every value is
#: a token — the mapping is the only place the two worlds meet.
_THEME_VARIABLES: dict[str, str] = {
    "background": token("bg"),
    "surface": token("panel.bg"),
    "panel": token("panel.bg"),
    "panel-border": token("panel.border"),
    "panel-title": token("panel.title"),
    "text-primary": token("text.primary"),
    "text-muted": token("text.muted"),
    "accent": token("accent"),
    "state-ok": token("state.ok"),
    "state-warn": token("state.warn"),
    "state-crit": token("state.crit"),
    "state-info": token("state.info"),
    "state-unknown": token("state.unknown"),
    "stale-warn": token("stale.warn"),
    "stale-crit": token("stale.crit"),
}

STAMMTISCH_THEME = Theme(
    name="stammtisch",
    dark=True,
    primary=token("accent"),
    secondary=token("state.info"),
    warning=token("state.warn"),
    error=token("state.crit"),
    success=token("state.ok"),
    accent=token("accent"),
    foreground=token("text.primary"),
    background=token("bg"),
    surface=token("panel.bg"),
    panel=token("panel.bg"),
    variables=_THEME_VARIABLES,
)

#: App-wide stylesheet: layout chrome only; panel bodies come from
#: ``render/panels.py`` as rich renderables.
_CSS = """
Screen {
    layout: vertical;
}
#banner {
    height: 1;
    color: $text-primary;
}
#collector-error {
    height: 1;
    color: $state-crit;
    display: none;
}
#wall {
    height: 1fr;
}
#runs {
    width: 1fr;
    border: round $panel-border;
    border-title-color: $panel-title;
    background: $surface;
}
#side {
    width: 42;
}
#glance, #services {
    height: auto;
    border: round $panel-border;
    border-title-color: $panel-title;
    padding: 0 1;
    color: $text-primary;
}
#feed {
    height: 12;
    border: round $panel-border;
    border-title-color: $panel-title;
}
#run-detail {
    align: center middle;
}
#run-detail-body {
    width: 96;
    max-height: 80%;
    border: round $panel-border;
    border-title-color: $panel-title;
    padding: 0 2;
    overflow: auto;
    background: $surface;
    color: $text-primary;
}
#runs.stale-warn, #glance.stale-warn, #services.stale-warn, #feed.stale-warn {
    border: round $stale-warn;
}
#runs.stale-crit, #glance.stale-crit, #services.stale-crit, #feed.stale-crit {
    border: round $stale-crit;
}
#filter {
    height: 3;
    display: none;
}
"""


class WorkstationShell(App[None]):
    """Thin shell: provider -> spine -> screens via the router. Owns wiring."""

    TITLE = "STAMMTISCH"
    CSS = _CSS
    # The workstation's palette carries the ONE command vocabulary
    # (router.run_command); Textual's built-in provider screen is disabled
    # so ctrl+p cannot open a second, drifting palette.
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit the workstation"),
        Binding("ctrl+p", "palette", "Command palette (recents first)"),
        Binding("colon", "command_bar", "Type a command (:runs, :backtest…)"),
        # Priority so the sheet reaches modal screens too (the non-priority
        # chain stops at the last modal). A focused Input still keeps `?` as
        # text: Textual strips Input-consumed keys from the binding chain
        # before priority dispatch — a filter regex can always contain `?`.
        Binding("question_mark", "help_overlay",
                "Key sheet (generated from the live bindings)", priority=True),
    ]

    def __init__(
        self,
        provider: SnapshotProvider | None = None,
        *,
        root: Path | None = None,
        demo: bool = False,
        interval_s: float = 1.0,
        clock: Callable[[], float] | None = None,
        delete_run: Callable[[str], CliEnvelope] | None = None,
    ) -> None:
        super().__init__()
        # Register before the first stylesheet parse so CSS `$variables`
        # resolve against this theme from the very first frame.
        self.register_theme(STAMMTISCH_THEME)
        self.theme = "stammtisch"
        self._root = root
        self._provider: SnapshotProvider = (
            provider if provider is not None else build_provider(root, demo=demo)
        )
        self._spine = RefreshSpine(
            self, self._provider, interval_s=interval_s, clock=clock or time.time
        )
        self._router = Router(self)
        # The ONE write lane (None = demo / no state root: confirming then
        # refuses honestly instead of faking a delete).
        self._delete_run = delete_run
        self._delete_flight = SingleFlight()
        self._register_routes()

    @property
    def spine(self) -> RefreshSpine:
        return self._spine

    @property
    def provider(self) -> SnapshotProvider:
        return self._provider

    @property
    def router(self) -> Router:
        return self._router

    def _register_routes(self) -> None:
        """The route table: name -> lazy factory (screens are built on goto).

        M6 additions: the first absorbed read-only screens (feeds /
        ledger / energy / polymarket / sentiment). The ledger gets the
        resolved state root here (composition-root wiring); the tapes
        resolve their own fail-closed egress lazily inside the screens.
        """
        self._router.register(ROOT_ROUTE, lambda: OverviewScreen())
        self._router.register(
            "workbench",
            lambda tool="fetch": WorkbenchScreen(tool=tool))
        self._router.register("detail", lambda run_id: RunsDetailScreen(run_id))
        self._router.register(
            "confirm_delete",
            lambda run_id: ConfirmDialog(run_id, self._delete_resolved))
        self._router.register("feeds", lambda: FeedsHealthScreen())
        self._router.register(
            "ledger", lambda: LedgerScreen(state_root=self._root))
        self._router.register("energy", lambda: EnergyScreen())
        self._router.register("polymarket", lambda: PolymarketScreen())
        self._router.register("sentiment", lambda: SentimentScreen())

    # -- RouterHost surface ------------------------------------------------

    def pop_to_root(self) -> None:
        """Pop every screen above the base overview (route ``runs``)."""
        while len(self.screen_stack) > 1:
            self.pop_screen()

    def current_frame(self) -> WorkstationSnapshot | None:
        return self._spine.last_frame

    # -- palette / command bar ----------------------------------------------

    def action_palette(self) -> None:
        self.push_screen(CommandPaletteScreen())

    def action_command_bar(self) -> None:
        self.push_screen(CommandBarScreen())

    def action_help_overlay(self) -> None:
        """``?`` — push the key sheet GENERATED from the live bindings.

        Rows are captured from the topmost screen's ``active_bindings``
        (the dispatcher's own chain: focused widget → screen → app)
        BEFORE the overlay is pushed, so the sheet describes the screen
        underneath, never itself. Nothing is hand-listed: a binding
        declared anywhere shows up without touching this module.

        The binding is priority (the only way it reaches modal screens),
        which also makes it fire while the sheet itself is open — hence
        the toggle: ``?`` closes the sheet it opened.

        The sheet NEVER stacks over the ConfirmDialog: Textual's
        ``Screen.dismiss()`` pops the TOPMOST screen unconditionally
        (textual/screen.py: ``dismiss`` → ``app.pop_screen()``), so a
        dialog resolving mid-stack (its fail-closed silence can fire
        while another modal covers it) would pop the WRONG screen and
        strand the resolved — inert, single-shot, unclosable — dialog on
        top. Refusing keeps the ONE write path's surface clean.
        """
        screen = self.screen
        if isinstance(screen, HelpOverlay):
            screen.dismiss(None)
            return
        if isinstance(screen, ConfirmDialog):
            self.notify("key sheet unavailable over the confirm dialog "
                        "(Esc cancels it)", severity="warning")
            return
        route = getattr(screen, "route_name", None)
        title = f"KEYS · {route}" if route else "KEYS"
        self.push_screen(
            HelpOverlay(help_rows(screen.active_bindings, self.get_key_display),
                        title=title))

    def get_default_screen(self) -> OverviewScreen:
        return OverviewScreen()

    def on_mount(self) -> None:
        self._spine.start()

    # -- the ONE write path: audited, confirmed, off-thread ─────────────

    def _audit(self, line: str) -> None:
        """One audit line into the base screen's activity feed.

        The feed is append-only chrome (same discipline as the follow
        marker): audit lines are never run events, and the write trail
        survives panel refreshes by construction.
        """
        if not self.screen_stack:
            return
        audit = getattr(self.screen_stack[0], "audit_line", None)
        if callable(audit):
            audit(line)

    def _delete_resolved(self, run_id: str, confirmed: bool) -> None:
        """The ConfirmDialog verdict: audit it; on confirm, run the write."""
        if not confirmed:
            self._audit(f"ui.delete cancelled run={run_id}")
            self.notify(f"delete cancelled: {run_id}")
            return
        self._audit(f"ui.delete confirmed run={run_id}")
        if self._delete_run is None:
            self.notify("delete refused: no core write lane "
                        "(demo or no state root)", severity="error")
            self._audit(f"core.delete refused run={run_id} "
                        "error=no core write lane")
            return
        if not self._delete_flight.try_start():
            self.notify("a delete is already in flight", severity="warning")
            return
        self.notify(f"deleting {run_id}\u2026")
        try:
            self.run_worker(
                lambda: self._delete_worker(run_id),
                name="delete-run", group="delete", thread=True,
            )
        except Exception:  # noqa: BLE001 - mirror the spine: release the slot
            self._delete_flight.finish()
            self.notify("delete dispatch failed; slot released",
                        severity="error")

    def _delete_worker(self, run_id: str) -> None:
        """Worker-thread body: one core delete, degraded to a verdict."""
        envelope: CliEnvelope | None = None
        error: str | None = None
        delete_run = self._delete_run
        try:
            if delete_run is not None:
                envelope = delete_run(run_id)
        except Exception as exc:  # noqa: BLE001 - notify + audit, never crash
            error = f"delete worker error: {exc!r}"
        try:
            self.call_from_thread(self._delete_landed, run_id, envelope, error)
        except Exception:  # noqa: BLE001, S110 - app torn down; land quietly
            pass

    def _delete_landed(
        self, run_id: str, envelope: CliEnvelope | None, error: str | None
    ) -> None:
        """UI-thread landing: free the slot, audit + notify, force refresh."""
        self._delete_flight.finish()
        if error is not None:
            self.notify(f"delete failed: {error}", severity="error")
            self._audit(f"core.delete error run={run_id} error={error}")
        elif envelope is not None and envelope.ok:
            # data.removed echoes the core's own bookkeeping into the trail.
            removed = envelope.data.get("removed")
            suffix = " (removed)" if removed else ""
            self.notify(f"deleted run {run_id}{suffix}")
            self._audit(f"core.delete ok run={run_id} removed={removed}")
        elif envelope is not None:
            self.notify(f"delete failed: {envelope.error_message}",
                        severity="error")
            self._audit(f"core.delete failed run={run_id} "
                        f"error={envelope.error_message}")
        self._spine.force_refresh()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="interface.app.shell",
        description="The STAMMTISCH terminal workstation.",
    )
    add_data_args(parser)
    parser.add_argument("--interval", type=float, default=1.0, metavar="SEC",
                        help="spine tick interval in seconds (default 1.0)")
    return parser.parse_args(argv)


def _build_delete_run(root: Path | None,
                      demo: bool) -> Callable[[str], CliEnvelope] | None:
    """The write lane: the real session's core client (demo has none).

    ``session_for`` is process-wide memoized, so this is the SAME client
    the snapshot polls use — one spawn accounting, one availability fact.
    """
    if demo or root is None:
        return None
    core = session_for(root, use_core=True).core
    return None if core is None else core.delete


def main(argv: list[str] | None = None) -> int:
    """Run the workstation app (returns 0 on clean exit)."""
    args = _parse_args(argv)
    root = resolve_state_root(args.root)
    app = WorkstationShell(
        root=root, demo=args.demo, interval_s=args.interval,
        delete_run=_build_delete_run(root, args.demo),
    )
    app.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
