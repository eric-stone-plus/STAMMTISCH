"""Chat and ask-session screens."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Input, OptionList, Static, TextArea
from rich.text import Text
from textual.widgets.option_list import Option

from ..ai_driver import AIDriver, ChatResponse

import logging

logger = logging.getLogger(__name__)

from .modals import ConfirmScreen
from .sessions import (
    _ASK_INTRO, _INPUT_HISTORY_CAP, _ask_dir, _atomic_write_json,
    _created_stamp, _new_ask_session_id, delete_ask_session,
    list_ask_sessions, load_ask_session, load_input_history,
    render_ask_session, save_ask_session, save_input_history,
)

# Clockwise box corners — not the Grok Build working spinner.
_LOAD_FRAMES = ("┌", "┐", "┘", "└")
_THINK_TICK = 0.12
def _thinking_line(elapsed: int, frame: int) -> str:
    return f"  {_LOAD_FRAMES[frame % len(_LOAD_FRAMES)]}  thinking  {elapsed}s\n"
def _empty_session(session_id: str | None = None) -> dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "schema": "stammtisch.ask-session.v1",
        "id": session_id or _new_ask_session_id(),
        "created_at": now,
        "updated_at": now,
        "title": "",
        "turns": [],
    }
class ChatInput(TextArea):
    """Multi-line chat box: Enter submits,
    Shift-Enter / Ctrl-J inserts a newline; height follows content.

    Up/Down move the caret inside the buffer. At the first/last line they
    step the sent-line history instead.
    """

    BINDINGS = [
        # The box keeps focus for the screen's lifetime, so the screen's
        # own PgUp/PgDn bindings never fire — route them from here.
        Binding("pageup", "screen.scroll_up", show=False),
        Binding("pagedown", "screen.scroll_down", show=False),
    ]

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    class HistoryStep(Message):
        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            if self.text.strip():
                self.post_message(self.Submitted(self.text))
            return
        if event.key in ("shift+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key == "up" and self.cursor_at_first_line:
            event.prevent_default()
            event.stop()
            self.post_message(self.HistoryStep(-1))
            return
        if event.key == "down" and self.cursor_at_last_line:
            event.prevent_default()
            event.stop()
            self.post_message(self.HistoryStep(1))
            return
        await super()._on_key(event)
class ChatScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("pageup", "scroll_up", "PgUp"),
        Binding("pagedown", "scroll_down", "PgDn"),
        Binding("ctrl+o", "open_sessions", "Sessions"),
        Binding("ctrl+n", "new_session", "New"),
    ]
    CSS = """
    ChatScreen { layout: vertical; }
    #chat-scroll {
        height: 1fr;
        border: solid #505050;
        background: #000000;
        padding: 1 1 1 2;
        overflow-x: hidden;
        overflow-y: scroll;
        scrollbar-gutter: stable;
        scrollbar-size-vertical: 1;
    }
    #chat-messages { height: auto; background: #000000; }
    #chat-input { height: 3; border: solid #4fc3f7; }
    """
    ai: AIDriver
    _chat_log: str = ""

    def __init__(
        self,
        ai: AIDriver,
        session_id: str | None = None,
        initial_prompt: str | None = None,
        initial_context: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.ai = ai
        self._asked_session_id = session_id
        # A screen can arrive with work to do (e.g. the captured daily
        # dataset handed to GALAHAD): fire one real chat turn on mount.
        self._initial_prompt = initial_prompt
        self._initial_context = initial_context
        self._ask_dir: Path | None = None
        self._session = _empty_session(session_id)
        self._chat_log = _ASK_INTRO
        self._pending: str | None = None
        self._pending_started = 0.0
        self._think_timer = None
        self._spin_frame = 0
        self._active_request: object | None = None
        self._input_history: list[str] = []
        self._history_index: int | None = None
        self._draft = ""

    def compose(self) -> ComposeResult:
        yield Static(
            "  Ask  |  Enter send · Shift-Enter newline · ↑↓ history  "
            "|  Ctrl+O sessions · Ctrl+N new  |  PgUp/PgDn  |  Esc back",
            classes="header-bar",
        )
        yield ScrollableContainer(
            Static(_ASK_INTRO, id="chat-messages", markup=False),
            id="chat-scroll",
        )
        yield ChatInput(id="chat-input")

    def on_mount(self) -> None:
        self._ask_dir = _ask_dir(self.app)
        self._input_history = load_input_history(self._ask_dir)
        # A opens a fresh turn. Prior conversations stay in Ctrl+O.
        loaded = None
        if self._asked_session_id:
            loaded = load_ask_session(self._ask_dir, str(self._asked_session_id))
        if loaded is not None:
            self._session = loaded
        self._chat_log = render_ask_session(self._session)
        self._sync_model_history()
        self._refresh_log()
        self.query_one("#chat-input", ChatInput).focus()
        if self._initial_prompt:
            prompt, self._initial_prompt = self._initial_prompt, None
            context, self._initial_context = self._initial_context, None
            self.call_after_refresh(self._submit, prompt, context)

    def _scroller(self) -> ScrollableContainer:
        return self.query_one("#chat-scroll", ScrollableContainer)

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_scroll_up(self) -> None:
        self._scroller().scroll_page_up()

    def action_scroll_down(self) -> None:
        self._scroller().scroll_page_down()

    def action_open_sessions(self) -> None:
        if self._ask_dir is None:
            self._ask_dir = _ask_dir(self.app)
        self.app.push_screen(AskSessionScreen(self._ask_dir, self._session.get("id")))

    def action_new_session(self) -> None:
        if self._active_request is not None:
            self.notify("Wait for the current answer before starting a new session.",
                        severity="warning")
            return
        self._persist()
        self._session = _empty_session()
        self._chat_log = _ASK_INTRO
        self._pending = None
        self._sync_model_history()
        self._persist()
        self._refresh_log()
        self.notify("New Ask session.")

    def open_session(self, session_id: str) -> None:
        if self._active_request is not None:
            self.notify("Wait for the current answer before switching sessions.",
                        severity="warning")
            return
        if self._ask_dir is None:
            self._ask_dir = _ask_dir(self.app)
        loaded = load_ask_session(self._ask_dir, session_id)
        if loaded is None:
            self.notify("Session not found.", severity="warning")
            return
        self._session = loaded
        self._chat_log = render_ask_session(loaded)
        self._pending = None
        self._sync_model_history()
        self._persist()
        self._refresh_log()

    def on_text_area_changed(self, event: ChatInput.Changed) -> None:
        lines = event.text_area.text.count("\n") + 1
        event.text_area.styles.height = min(10, max(3, lines + 2))

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        if self._active_request is not None:
            self.notify("Wait for the current answer before sending another.", severity="warning")
            return
        box = self.query_one("#chat-input", ChatInput)
        box.text = ""
        box.styles.height = 3
        self._submit(event.value.strip())

    def on_chat_input_history_step(self, event: ChatInput.HistoryStep) -> None:
        if not self._input_history:
            return
        box = self.query_one("#chat-input", ChatInput)
        if self._history_index is None:
            self._draft = box.text
            self._history_index = len(self._input_history)
        nxt = self._history_index + event.delta
        nxt = max(0, min(len(self._input_history), nxt))
        self._history_index = nxt
        text = self._draft if nxt == len(self._input_history) else self._input_history[nxt]
        box.text = text
        lines = text.count("\n") + 1
        box.styles.height = min(10, max(3, lines + 2))
        rows = text.split("\n") or [""]
        box.move_cursor((len(rows) - 1, len(rows[-1])))

    def _remember_input(self, query: str) -> None:
        if not query:
            return
        if not self._input_history or self._input_history[-1] != query:
            self._input_history.append(query)
            self._input_history = self._input_history[-_INPUT_HISTORY_CAP:]
        self._history_index = None
        self._draft = ""
        if self._ask_dir is not None:
            save_input_history(self._ask_dir, self._input_history)

    def _refresh_log(self) -> None:
        text = self._chat_log + (self._pending or "")
        self.query_one("#chat-messages", Static).update(text)
        # Follow the tail: new turns must stay visible without manual
        # scrolling; without this the viewport pins to the top of a growing
        # log and new answers look like the conversation stalled.
        self._scroller().scroll_end(animate=False)

    def _pending_events_tail(self, limit: int = 4) -> str:
        """Live tool-call stream under the spinner; deep-verification turns
        run minutes and a bare timer reads as hung."""
        events = getattr(self, "_live_events", [])
        if not events:
            return ""
        shown = events[-limit:]
        rows = [f"  | {event}" for event in shown]
        extra = len(events) - limit
        if extra > 0:
            rows.insert(0, f"  | ... {extra} earlier calls")
        return "\n" + "\n".join(rows) + "\n"


    def _tick_thinking(self) -> None:
        if self._active_request is None:
            return
        self._spin_frame += 1
        elapsed = int(time.monotonic() - self._pending_started)
        self._pending = _thinking_line(elapsed, self._spin_frame) + self._pending_events_tail()
        self._refresh_log()

    def _stop_thinking(self, request_token: object | None = None) -> None:
        if (
            request_token is not None
            and self._active_request is not request_token
        ):
            return
        timer = getattr(self, "_think_timer", None)
        if timer is not None:
            timer.stop()
            self._think_timer = None
        self._pending = None

    def _persist(self) -> None:
        if self._ask_dir is None:
            return
        save_ask_session(self._ask_dir, self._session)
        try:
            _atomic_write_json(
                self._ask_dir / "current.json",
                {"id": self._session.get("id")},
            )
        except OSError:
            pass

    def _sync_model_history(self) -> None:
        if not hasattr(self.ai, "history"):
            return
        try:
            from ..ai_driver import SYSTEM_PROMPT, ChatMessage
            messages = [ChatMessage("system", SYSTEM_PROMPT)]
            for turn in self._session.get("turns") or []:
                if not isinstance(turn, dict):
                    continue
                role = turn.get("role")
                content = str(turn.get("content") or "")
                if role in ("user", "assistant") and content:
                    messages.append(ChatMessage(str(role), content))
            lock = getattr(self.ai, "_lock", None)
            if lock is not None:
                with lock:
                    self.ai.history = messages
            else:
                self.ai.history = messages
        except Exception:
            pass

    def on_unmount(self) -> None:
        self._stop_thinking()
        self._active_request = None
        self._persist()

    def _submit(self, query: str, context: str | None = None) -> bool:
        if not query:
            return False
        if self._active_request is not None:
            self.notify("Wait for the current answer before sending another.", severity="warning")
            return False

        self._remember_input(query)
        request_token = object()
        self._active_request = request_token
        if not self._session.get("title"):
            self._session["title"] = query.splitlines()[0][:80]
        self._session.setdefault("turns", []).append({"role": "user", "content": query})
        self._chat_log = render_ask_session(self._session)
        self._pending_started = time.monotonic()
        self._spin_frame = 0
        self._pending = _thinking_line(0, 0)
        self._think_timer = self.set_interval(_THINK_TICK, self._tick_thinking)
        self._persist()
        box = self.query_one("#chat-input", ChatInput)
        box.disabled = True
        self._refresh_log()

        def _on_tool_event(event: str) -> None:
            def _ui() -> None:
                if self.is_mounted and self._active_request is not None:
                    self._live_events = getattr(self, "_live_events", [])[-20:] + [event]
            try:
                self.app.call_from_thread(_ui)
            except Exception:
                pass

        def _query():
            try:
                r = self.ai.chat(query, context=context, on_event=_on_tool_event)
            except Exception as e:
                r = ChatResponse(content="", error=str(e))
            result = r.content if r.ok else f"Error: {r.error}"
            def _update():
                if (
                    not self.is_mounted
                    or self._active_request is not request_token
                ):
                    return
                elapsed = int(time.monotonic() - self._pending_started)
                self._stop_thinking(request_token)
                # Reasoning stays collapsed: only elapsed time is kept.
                self._session.setdefault("turns", []).append({
                    "role": "assistant",
                    "content": result,
                    "elapsed_s": elapsed,
                    "tool_events": list(r.tool_events or []),
                })
                self._chat_log = render_ask_session(self._session)
                self._active_request = None
                self._persist()
                box = self.query_one("#chat-input", ChatInput)
                box.disabled = False
                box.focus()
                self._refresh_log()
            try:
                self.app.call_from_thread(_update)
            except Exception:
                pass
        threading.Thread(target=_query, daemon=True).start()
        return True
class AskSessionScreen(Screen):
    """Search and reopen persisted Ask sessions."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("enter", "open_selected", "Open"),
        Binding("x", "delete_selected", "Delete"),
    ]
    CSS = """
    AskSessionScreen { layout: vertical; }
    #ask-query { height: 3; border: solid #4fc3f7; }
    #ask-sessions { height: 1fr; border: solid #505050; background: #000000; }
    """

    def __init__(self, ask_dir: Path, current_id: Any = None, **kwargs: Any):
        super().__init__(**kwargs)
        self.ask_dir = ask_dir
        self.current_id = str(current_id or "")
        self._rows: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield Static(
            "  ASK SESSIONS  |  type to search  |  Enter open  |  X delete  |  Esc back",
            classes="header-bar",
        )
        yield Input(placeholder="search title or text", id="ask-query")
        yield OptionList(id="ask-sessions")

    def on_mount(self) -> None:
        self._reload()
        self.query_one("#ask-query", Input).focus()

    def _reload(self, query: str = "") -> None:
        needle = query.strip().lower()
        rows = list_ask_sessions(self.ask_dir)
        if needle:
            rows = [row for row in rows if needle in row["hay"]]
        self._rows = rows
        options = self.query_one("#ask-sessions", OptionList)
        options.clear_options()
        if not rows:
            options.add_option(Option(Text("  No matching sessions."), id="empty", disabled=True))
            return
        app_config = getattr(self.app, "config", None)
        tz_name = ""
        if app_config is not None:
            try:
                tz_name = str(app_config.get("display_timezone") or "")
            except Exception:
                tz_name = ""
        for row in rows:
            mark = "*" if row["id"] == self.current_id else " "
            stamp = _created_stamp(row["updated_at"], tz_name) or row["updated_at"]
            title = row["title"].replace("\n", " ")[:60]
            options.add_option(
                Option(Text(f" {mark} {stamp}  {title}  ({row['turns']})"), id=row["id"])
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "ask-query":
            self._reload(event.value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self._open(event.option_id)

    def action_open_selected(self) -> None:
        options = self.query_one("#ask-sessions", OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            return
        option = options.get_option_at_index(highlighted)
        self._open(option.id)

    def action_delete_selected(self) -> None:
        options = self.query_one("#ask-sessions", OptionList)
        highlighted = options.highlighted
        if highlighted is None:
            return
        option = options.get_option_at_index(highlighted)
        sid = option.id
        if not sid or sid == "empty":
            return

        def _confirmed() -> None:
            delete_ask_session(self.ask_dir, str(sid))
            self._reload(self.query_one("#ask-query", Input).value)
            self.notify(f"Deleted session {str(sid)[:16]}")

        self.app.push_screen(ConfirmScreen(
            f"  Delete Ask session {str(sid)[:16]}…?\n\n"
            f"  [Y] Confirm  [N]/[Esc] Cancel",
            _confirmed,
        ))

    def _open(self, session_id: str | None) -> None:
        if not session_id or session_id == "empty":
            return
        self.app.pop_screen()
        screen = self.app.screen
        if isinstance(screen, ChatScreen):
            screen.open_session(session_id)

    def action_back(self) -> None:
        self.app.pop_screen()
