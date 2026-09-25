"""The fail-closed confirm modal for the ONE write path (``:delete <run-id>``).

The interface is read-first; deleting a run through the core CLI is its
single explicit, audited write, and this modal is the gate the command
vocabulary routes to. Its contract (every clause fail-closed):

- CANCEL is the focused default. left/right cycle focus between the two
  buttons; Enter activates whichever button is focused.
- Esc resolves CANCEL. So does ``silence_s`` (30s) of no keys at all —
  any key re-arms the timer. Silence never action.
- The dialog PERFORMS NO WRITE ITSELF: on an explicit Confirm press it
  dismisses and hands ``(run_id, confirmed)`` to the ``on_resolved``
  callback — the shell audits both outcomes into the activity feed
  (``ui.delete confirmed/cancelled run=<id>``) and runs the single-flight
  delete worker only on a confirmed verdict.
- Resolution is single-shot: a racing Esc/silence/press cannot resolve
  the dialog twice.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Static

from interface.render.tokens import token

__all__ = ["CONFIRM_SILENCE_S", "ConfirmDialog"]

#: Default silence window: no key for this long resolves CANCEL.
CONFIRM_SILENCE_S = 30.0


class ConfirmDialog(ModalScreen[None]):
    """``Delete run <id>?`` — Cancel by default, audited either way."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel (never deletes)"),
        Binding("left", "cycle_prev", "Focus Cancel"),
        Binding("right", "cycle_next", "Focus Confirm"),
    ]
    CSS = """
    #confirm-wrap { align: center middle; }
    #confirm-box {
        width: 64; height: auto;
        border: round $panel-border;
        border-title-color: $panel-title;
        background: $surface;
        padding: 0 2;
        color: $text-primary;
    }
    #confirm-text { padding: 0 1; }
    #confirm-buttons { height: auto; align: center middle; padding: 0 1; }
    #confirm-cancel { margin: 0 1; }
    #confirm-confirm { margin: 0 1; }
    """

    def __init__(
        self,
        run_id: str,
        on_resolved: Callable[[str, bool], None],
        *,
        silence_s: float = CONFIRM_SILENCE_S,
    ) -> None:
        super().__init__()
        self.run_id = run_id
        self._on_resolved = on_resolved
        self._silence_s = silence_s
        self._silence_timer = None
        self._resolved = False
        #: Test observability: which button holds focus, per change.
        self.focus_history: list[str] = []
        #: Test observability: (source, confirmed) of the one resolution.
        self.last_resolution: tuple[str, bool] | None = None

    def compose(self):
        with Container(id="confirm-wrap"), Container(id="confirm-box"):
            yield Static(self._body_text(), id="confirm-text")
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="confirm-cancel")
                yield Button("Delete", id="confirm-confirm")
        yield Footer()

    def _body_text(self) -> Text:
        body = Text()
        body.append("Delete run ", style=token("text.primary"))
        body.append(self.run_id, style=f"bold {token('state.crit')}")
        body.append("?\n", style=token("text.primary"))
        body.append(
            "The core removes the run directory and its events. "
            "Irreversible; the outcome is audited either way.\n"
            "Cancel is the default; Esc or 30s of silence also cancel.",
            style=token("text.muted"),
        )
        return body

    def on_mount(self) -> None:
        self.query_one("#confirm-box", Container).border_title = (
            "CONFIRM DELETE")
        # Cancel focused by default: Enter alone must NEVER delete.
        self._focus_button("#confirm-cancel")
        self._arm_silence()

    # -- focus cycling -----------------------------------------------------

    @property
    def _buttons(self) -> list[Button]:
        return [
            self.query_one("#confirm-cancel", Button),
            self.query_one("#confirm-confirm", Button),
        ]

    def _focus_button(self, selector: str) -> None:
        button = self.query_one(selector, Button)
        button.focus()
        self.focus_history.append(selector)

    def action_cycle_prev(self) -> None:
        self._cycle(-1)

    def action_cycle_next(self) -> None:
        self._cycle(1)

    def _cycle(self, offset: int) -> None:
        buttons = self._buttons
        focused = self.focused
        index = buttons.index(focused) if focused in buttons else 0
        target = buttons[(index + offset) % len(buttons)]
        target.focus()
        self.focus_history.append(
            "#confirm-cancel" if target is buttons[0] else "#confirm-confirm")

    # -- silence: 30s of no keys resolves CANCEL, never action -------------

    def _arm_silence(self) -> None:
        if self._silence_timer is not None:
            self._silence_timer.stop()
            self._silence_timer = None
        if self._silence_s > 0:
            self._silence_timer = self.set_timer(
                self._silence_s, self._silence_cancel)

    def _silence_cancel(self) -> None:
        self._silence_timer = None
        self._resolve(False, source="silence")

    def on_key(self, event: events.Key) -> None:
        """Any key is activity: re-arm the silence window."""
        self._arm_silence()

    # -- resolution (single-shot) -------------------------------------------

    def action_cancel(self) -> None:
        self._resolve(False, source="esc")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._resolve(event.button.id == "confirm-confirm", source="button")

    def _resolve(self, confirmed: bool, *, source: str) -> None:
        """Resolve exactly once; dismiss first, then notify the shell."""
        if self._resolved:
            return
        self._resolved = True
        self.last_resolution = (source, confirmed)
        if self._silence_timer is not None:
            self._silence_timer.stop()
            self._silence_timer = None
        # Defense in depth (round F, S-1): ``Screen.dismiss()`` pops the
        # TOPMOST screen unconditionally (textual/screen.py: dismiss →
        # ``app.pop_screen()``). Were anything covering the dialog when it
        # resolves (the shell's help guard makes that unreachable), a
        # dismiss would pop the WRONG screen and strand this spent dialog
        # on top — inert (single-shot) and unclosable (Esc cannot
        # re-resolve). Never pop a screen that is not ours.
        if self.app.screen is self:
            self.dismiss(None)
        try:
            self._on_resolved(self.run_id, confirmed)
        except Exception:  # noqa: BLE001 - never let an audit crash the UI
            from textual.app import App

            App.log.error("confirm callback failed", exc_info=True)
