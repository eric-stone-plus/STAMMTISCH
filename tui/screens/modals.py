"""Generic modal screens (confirm, help)."""

from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Static


import logging

logger = logging.getLogger(__name__)

class ConfirmScreen(Screen):
    """Yes/No modal for destructive actions — keyboard AND mouse.

    Keyboard: y/Enter confirm, n/Esc cancel, ←/→ switch focus.
    Mouse: real buttons (the confirm button gets focus on mount, so
    Tab/Enter also work).
    """

    BINDINGS = [
        Binding("y", "confirm", "Confirm"),
        Binding("enter", "confirm", "Confirm"),
        Binding("n", "cancel", "Cancel"),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("left", "left", "←", show=False),
        Binding("right", "right", "→", show=False),
    ]
    CSS = """
    ConfirmScreen { align: center middle; background: rgba(0,0,0,0.9); }
    #confirm-box {
        width: 50; height: auto;
        background: #000000;
        border: heavy #ffffff; padding: 1 2;
        align: center middle;
    }
    #confirm-text { color: #ffffff; text-style: bold; text-align: center; width: 100%; }
    #confirm-buttons { height: 3; padding: 0 1; align-horizontal: center; width: 100%; }
    #confirm-buttons Button {
        margin: 0 1; min-width: 16;
        background: #000000; border: heavy #555555; color: #888888;
    }
    #confirm-buttons Button:focus {
        border: heavy #ffffff; color: #ffffff;
    }
    """

    def __init__(self, prompt: str, on_confirm, **kwargs: Any):
        super().__init__(**kwargs)
        self.prompt = prompt
        self.on_confirm = on_confirm

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.prompt, id="confirm-text")
            with Horizontal(id="confirm-buttons"):
                yield Button("Y — Confirm", variant="error", id="confirm-yes")
                yield Button("N — Cancel", variant="default", id="confirm-no")

    def on_mount(self) -> None:
        # Focus the destructive button so the screen has an interactive
        # focus target from the first keystroke.
        self.query_one("#confirm-yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._finish(event.button.id == "confirm-yes")

    def action_confirm(self) -> None:
        self._finish(True)

    def action_cancel(self) -> None:
        self._finish(False)

    def action_focus_yes(self) -> None:
        """Focus the Y button"""
        self.query_one("#confirm-yes", Button).focus()

    def action_focus_no(self) -> None:
        """Focus the N button"""
        self.query_one("#confirm-no", Button).focus()

    def _focused_id(self) -> str:
        focused = self.app.focused
        return focused.id if focused else ""

    def action_left(self) -> None:
        """Left key: cycle focus between the buttons"""
        if self._focused_id() == "confirm-yes":
            self.action_focus_no()
        else:
            self.action_focus_yes()

    def action_right(self) -> None:
        """Right key: cycle focus between the buttons"""
        if self._focused_id() == "confirm-no":
            self.action_focus_yes()
        else:
            self.action_focus_no()

    def _finish(self, confirmed: bool) -> None:
        self.app.pop_screen()
        if confirmed:
            self.on_confirm()
class KeyHelpScreen(Screen):
    """One screen's keybinding reference.

    The shell is shared; the rows are supplied by the screen that pushes
    it, so every board documents its own keys instead of inheriting the
    dashboard's.
    """

    BINDINGS = [("escape", "back", "Close")]
    CSS = (
        "KeyHelpScreen { align: center middle; } "
        "#keyhelp-box { width: 64; height: auto; max-height: 80%; "
        "background: #000000; border: solid #505050; padding: 1 2; }"
    )

    def __init__(self, title: str, rows: list[tuple[str, str]], **kwargs: Any):
        super().__init__(**kwargs)
        self.title = title
        self.rows = rows

    def compose(self) -> ComposeResult:
        lines = [f"  {self.title}", ""]
        lines += [f"    {key:<16} {desc}" for key, desc in self.rows]
        lines += ["", "  Press Esc to close"]
        with Vertical(id="keyhelp-box"):
            yield Static("\n".join(lines))

    def action_back(self) -> None:
        self.app.pop_screen()


class HelpScreen(Screen):
    BINDINGS = [("escape", "back", "Close"), ("q", "back", "Close")]
    CSS = "HelpScreen { align: center middle; } #help-box { width: 70; height: auto; max-height: 80%; background: #000000; border: solid #505050; padding: 1 2; }"

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static(
                "  STAMMTISCH Help\n\n"
                "  Plugins (middle list, alphabetical): arrows / Enter / click.\n"
                "    SECURITY (equity board by market zone, ←/→ switches,\n"
                "    quant + daily hotkeys),\n"
                "    CRYPTO (Polymarket tape), ENERGY (EIA watchlist, read-only),\n"
                "    FUTURES (category board: continuous + exchange-settled,\n"
                "    ←/→ switches category, K = browser chart, B/F/T/P quant),\n"
                "    CASINO (wagerkit race board via racing_cmd),\n"
                "    SHIPPING (exchange settlement board via shipping_cmd),\n"
                "    other domain plugins from config 'plugins' (directory view).\n\n"
                "  Quick Start (bottom list):\n"
                "    a  Ask (GALAHAD chat)      e  Edit config\n\n"
                "  Dashboard keys: a Ask · e Edit · Del delete · Shift+D delete all ·\n"
                "    Esc Quit (with confirmation).\n"
                "    Click selects one run; Shift+click selects the range; Ctrl+A selects all.\n"
                "    Enter inspects the selected run. Session titles are pipeline + time.\n"
                "    FULLSTACK is the example pipeline, not a sidebar workbench.\n\n"
                "  Chat: Enter send · Shift-Enter newline · ↑↓ history · Ctrl+O sessions\n"
                "    Ctrl+N new session · PgUp/PgDn scroll. Thinking is collapsed with time.\n\n"
                "  Daily: R captures the last session before the A-share open,\n"
                "    otherwise today's session. Progress stays on Home.\n\n"
                "  Press Esc to close"
            )

    def action_back(self) -> None:
        self.app.pop_screen()
