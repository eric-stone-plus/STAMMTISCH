"""Main STAMMTISCH TUI — command-driven interface."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer

from .config import Config
from .driver import StammtischDriver
from .deepseek import DeepSeekDriver
from .engine import QuantEngine
from .theme import THEME_CSS


class StammtischTUI(App):
    TITLE = "STAMMTISCH QUANT WORKSTATION"
    CSS = THEME_CSS
    BINDINGS = [
        Binding("q", "quit", "Quit", show=False),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("question_mark", "show_help", "Help"),
        Binding("escape", "escape_key", "Back", show=False),
    ]

    def __init__(self, **kwargs: Any):
        self.config = Config()
        binary = kwargs.pop("binary", None)
        state_root = kwargs.pop("state_root", None) or self.config.state_root
        pipeline_dir = kwargs.pop("pipeline_dir", None)
        deepseek_key = kwargs.pop("deepseek_key", None) or self.config.deepseek_api_key
        kwargs.pop("skip_boot", None)

        super().__init__(**kwargs)

        self.driver = StammtischDriver(binary=binary, state_root=state_root, pipeline_dir=pipeline_dir)
        self.engine = QuantEngine(data_dir=self.config.data_dir)
        from .tools import default_tools
        self.ai = DeepSeekDriver(
            api_key=deepseek_key,
            base_url=self.config.deepseek_base_url,
            model=self.config.deepseek_model,
            tools=default_tools(self.engine),
        )
        # Populated only after IntakeDriver has verified the complete artifact
        # graph. Downstream sentiment may reuse this exact report in-process;
        # it must not rediscover an unverified JSON file by filename.
        self.last_daily_intake_result = None
        from .intake_job import IntakeSupervisor
        self.intake_supervisor = IntakeSupervisor()

    def on_mount(self) -> None:
        from .screens import DashboardScreen
        self.driver.prepare()
        self.push_screen(DashboardScreen(self.driver, self.ai, self.engine, self.config))
        # Resident auto-capture: GALAHAD judges from a digest whether a
        # daily-data capture is due; deterministic gates run first.
        from .intake_steward import steward_for
        steward_for(self).start(self)

    def on_unmount(self) -> None:
        """Reap local core/chart/intake processes owned by this application."""
        from .intake_steward import steward_for
        steward_for(self).stop()
        self.driver.close()
        from .chart_server import stop_owned_server
        stop_owned_server()
        from .subproc import stop_owned
        stop_owned()

    def compose(self) -> ComposeResult:
        yield Footer()

    def action_show_help(self) -> None:
        from .screens import HelpScreen
        self.push_screen(HelpScreen())

    def action_escape_key(self) -> None:
        """Escape: go back if on sub-screen, do nothing on main screen."""
        from .screens import DashboardScreen
        if not isinstance(self.screen, DashboardScreen):
            self.pop_screen()


def run_tui(**kwargs: Any) -> None:
    StammtischTUI(**kwargs).run()
