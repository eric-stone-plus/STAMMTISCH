"""Workstation configuration screen."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Select, Static
from rich.text import Text

from ..config import AI_PROFILES, ai_profile_for_base_url
from ..driver import StammtischDriver
from ..ai_driver import AIDriver
from ..engine import QuantEngine

import logging

logger = logging.getLogger(__name__)

class ConfigScreen(Screen):
    """Edit workstation config — API key, model, workspace, backtest defaults."""

    PROXY_POLICY_KEYS = [
        ("proxy.off", "off"),
        ("proxy.all", "all"),
        ("proxy.poly", "poly"),
        ("proxy.energy", "energy"),
        ("proxy.custom", "custom"),
    ]
    DEFAULT_EGRESS_PORT = "17878"

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+s", "save", "Save"),
        Binding("left", "prev_page", "Prev Page", priority=True),
        Binding("right", "next_page", "Next Page", priority=True),
    ]
    CSS = """
    ConfigScreen { layout: vertical; }
    #cfg-scroll { height: 1fr; }
    #cfg-pages { height: 1; padding: 0 1; background: #202020; }
    .cfg-page-off { display: none; }
    .cfg-row { height: 3; }
    .cfg-label { width: 16; color: #a0a0a0; padding: 1 0; }
    .cfg-input { width: 1fr; }
    .cfg-row Select { width: 1fr; }
    #cfg-buttons { height: 3; padding: 0 1; align-horizontal: right; }
    #cfg-buttons Button { margin: 0 0 0 1; }
    """

    config: Any
    ai: AIDriver
    driver: StammtischDriver
    engine: QuantEngine

    def __init__(self, config: Any, ai: AIDriver, driver: StammtischDriver,
                 engine: QuantEngine, **kwargs: Any):
        super().__init__(**kwargs)
        self.config = config
        self.ai = ai
        self.driver = driver
        self.engine = engine
        keys = config.get("ai_profile_keys")
        self._profile_keys: dict[str, str] = dict(keys) if isinstance(keys, dict) else {}

    def _provider_value(self) -> str | None:
        """Profile name matching the configured base URL, if any."""
        return ai_profile_for_base_url(str(self.config.get("ai_base_url") or ""))

    def _stash_current_key(self) -> None:
        """Remember the key field's value under the profile the Base URL field
        currently points at, so switching away never loses a key."""
        profile = ai_profile_for_base_url(
            self.query_one("#cfg-base-url", Input).value.strip()
        )
        key = self.query_one("#cfg-key", Input).value.strip()
        if profile and key:
            self._profile_keys[profile] = key

    @property
    def _language(self) -> str:
        try:
            language = str(self.config.get("language", "en") or "en")
        except Exception:
            language = "en"
        return "zh" if language == "zh" else "en"

    def _chrome(self, key: str, fallback: str) -> str:
        from ..lang import tr

        return tr(self._language, key, fallback)

    def _policy_value(self) -> str:
        poly = str(self.config.get("polymarket_proxy_url", "") or "")
        energy = str(self.config.get("energy_proxy_url", "") or "")

        def local(url: str) -> bool:
            return url.startswith("http://127.0.0.1:")

        if poly and energy:
            return "all" if local(poly) and local(energy) else "custom"
        if poly:
            return "poly" if local(poly) else "custom"
        if energy:
            return "energy" if local(energy) else "custom"
        return "off"

    def _egress_port(self) -> str:
        poly = str(self.config.get("polymarket_proxy_url", "") or "")
        if poly.startswith("http://127.0.0.1:"):
            port = poly.rsplit(":", 1)[-1]
            if port.isdigit():
                return port
        return self.DEFAULT_EGRESS_PORT

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "cfg-provider" and event.value:
            name = str(event.value)
            _label, base, model = AI_PROFILES[name]
            self._stash_current_key()
            self.query_one("#cfg-base-url", Input).value = base
            self.query_one("#cfg-model", Input).value = model
            self.query_one("#cfg-key", Input).value = self._profile_keys.get(name, "")
        elif event.select.id == "cfg-policy":
            self._apply_proxy_policy(event.value)

    def _apply_proxy_policy(self, policy: str) -> None:
        port = (
            self.query_one("#cfg-egress-port", Input).value.strip()
            or self.DEFAULT_EGRESS_PORT
        )
        url = f"http://127.0.0.1:{port}"
        poly = self.query_one("#cfg-polymarket-proxy", Input)
        energy = self.query_one("#cfg-energy-proxy", Input)
        if policy == "off":
            poly.value = ""
            energy.value = ""
        elif policy == "all":
            poly.value = url
            energy.value = url
        elif policy == "poly":
            poly.value = url
            energy.value = ""
        elif policy == "energy":
            poly.value = ""
            energy.value = url
        # "custom": leave both fields exactly as they are.

    def compose(self) -> ComposeResult:
        yield Static("  Config  |  [←→] Page  [Ctrl+S] Save  [Esc] Cancel",
                     classes="header-bar")
        yield Static("", id="cfg-pages")
        with ScrollableContainer(id="cfg-scroll"):
            with Vertical(classes="panel cfg-pg cfg-page-1"):
                yield Static(self._chrome("config.ai", "AI Service"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Provider", classes="cfg-label")
                    yield Select(
                        [("Custom", None)]
                        + [(label, name) for name, (label, _base, _model) in AI_PROFILES.items()],
                        value=self._provider_value(),
                        allow_blank=False,
                        id="cfg-provider",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  API Key", classes="cfg-label")
                    yield Input(value=self.config.get("ai_api_key", ""), password=True,
                                placeholder="sk-...", id="cfg-key", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Base URL", classes="cfg-label")
                    yield Input(value=self.config.ai_base_url, id="cfg-base-url", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Model", classes="cfg-label")
                    yield Input(value=self.config.ai_model, id="cfg-model", classes="cfg-input")
            with Vertical(classes="panel cfg-pg cfg-page-2"):
                yield Static(self._chrome("config.workspace", "Workspace"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  State Root", classes="cfg-label")
                    yield Input(value=self.config.state_root or "",
                                placeholder="~/.local/share/stammtisch", id="cfg-state-root", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Data Dir", classes="cfg-label")
                    yield Input(value=self.config.data_dir or "", id="cfg-data-dir", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Chart Port", classes="cfg-label")
                    yield Input(value=str(self.config.get("chart_port", 0)),
                                placeholder="0 = auto",
                                id="cfg-chart-port", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Timezone", classes="cfg-label")
                    yield Input(value=self.config.get("display_timezone", ""),
                                placeholder="e.g. Asia/Shanghai (empty = UTC)",
                                id="cfg-timezone", classes="cfg-input")
            with Vertical(classes="panel cfg-pg cfg-page-3"):
                yield Static(self._chrome("config.network", "Egress Proxies"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Policy", classes="cfg-label")
                    yield Select(
                        [
                            (self._chrome(key, value), value)
                            for key, value in self.PROXY_POLICY_KEYS
                        ],
                        value=self._policy_value(),
                        allow_blank=False,
                        id="cfg-policy",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Port", classes="cfg-label")
                    yield Input(value=self._egress_port(),
                                placeholder=self.DEFAULT_EGRESS_PORT,
                                id="cfg-egress-port", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Market Proxy", classes="cfg-label")
                    yield Input(
                        value=self.config.get("polymarket_proxy_url", ""),
                        placeholder="http://127.0.0.1:PORT (empty = off)",
                        id="cfg-polymarket-proxy",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Energy Proxy", classes="cfg-label")
                    yield Input(
                        value=self.config.get("energy_proxy_url", ""),
                        placeholder="http://127.0.0.1:PORT (empty = off)",
                        id="cfg-energy-proxy",
                        classes="cfg-input",
                    )
            with Vertical(classes="panel cfg-pg cfg-page-2"):
                yield Static(self._chrome("config.energy", "Energy (EIA)"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  EIA API Key", classes="cfg-label")
                    yield Input(value=self.config.get("eia_api_key", ""), password=True,
                                placeholder="register free at eia.gov/opendata",
                                id="cfg-eia-key", classes="cfg-input")
            with Vertical(classes="panel cfg-pg cfg-page-4"):
                yield Static(self._chrome("config.intake", "Daily Data Intake"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Intake Cmd", classes="cfg-label")
                    yield Input(
                        value=self.config.get("intake_cmd", ""),
                        placeholder="daily-data product command",
                        id="cfg-intake-cmd",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Data Workspace", classes="cfg-label")
                    yield Input(
                        value=self.config.workspace_root,
                        placeholder="~/.local/share/stammtisch/daily-data",
                        id="cfg-workspace-root",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Timeout (sec)", classes="cfg-label")
                    yield Input(
                        value=str(self.config.intake_timeout_seconds),
                        id="cfg-intake-timeout",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Report Builder", classes="cfg-label")
                    yield Input(
                        value=self.config.get("intake_report_builder", "ai"),
                        placeholder="ai or deterministic",
                        id="cfg-intake-builder",
                        classes="cfg-input",
                    )
            with Vertical(classes="panel cfg-pg cfg-page-4"):
                yield Static(self._chrome("config.forecast", "Forecast"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Forecast Cmd", classes="cfg-label")
                    yield Input(value=self.config.get("kronos_cmd", ""),
                                placeholder="e.g. kronos-forecast (empty = off)",
                                id="cfg-kronos-cmd", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Horizon", classes="cfg-label")
                    yield Input(value=str(self.config.get("kronos_horizon", 20)),
                                id="cfg-kronos-horizon", classes="cfg-input")
            with Vertical(classes="panel cfg-pg cfg-page-5"):
                yield Static(self._chrome("config.domains", "Domains"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Security Symbols", classes="cfg-label")
                    yield Input(
                        value=" ".join(self.config.security_symbols),
                        placeholder="601088.SS 1088.HK BTU (space or comma separated)",
                        id="cfg-security-symbols",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Futures Symbols", classes="cfg-label")
                    yield Input(
                        value=" ".join(self.config.futures_symbols),
                        placeholder="BZ=F CL=F (space or comma separated)",
                        id="cfg-futures-symbols",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Futures Cmd", classes="cfg-label")
                    yield Input(
                        value=self.config.get("futures_cmd", ""),
                        placeholder="fuel board command (empty = off)",
                        id="cfg-futures-cmd",
                        classes="cfg-input",
                    )
                with Horizontal(classes="cfg-row"):
                    yield Static("  Shipping Cmd", classes="cfg-label")
                    yield Input(
                        value=self.config.get("shipping_cmd", ""),
                        placeholder="sgx board command (empty = directory view)",
                        id="cfg-shipping-cmd",
                        classes="cfg-input",
                    )
            with Vertical(classes="panel cfg-pg cfg-page-6"):
                yield Static(self._chrome("config.backtest", "Backtest Defaults"), classes="panel-title")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Strategy", classes="cfg-label")
                    yield Input(value=self.config.default_strategy, id="cfg-strategy", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Fast", classes="cfg-label")
                    yield Input(value=str(self.config.default_fast), id="cfg-fast", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Slow", classes="cfg-label")
                    yield Input(value=str(self.config.default_slow), id="cfg-slow", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Cost Tier", classes="cfg-label")
                    yield Input(value=self.config.default_cost_tier, id="cfg-cost-tier", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Rebalance", classes="cfg-label")
                    yield Input(value=self.config.default_rebalance, id="cfg-rebalance", classes="cfg-input")
                with Horizontal(classes="cfg-row"):
                    yield Static("  Lookback", classes="cfg-label")
                    yield Input(value=str(self.config.default_lookback), id="cfg-lookback", classes="cfg-input")
        with Horizontal(id="cfg-buttons"):
            yield Button("Save", id="cfg-save")
            yield Button("Cancel", id="cfg-cancel")
        yield Footer()

    PAGE_TITLES = {1: "AI", 2: "WORKSPACE", 3: "NETWORK", 4: "DATA",
                   5: "DOMAINS", 6: "BACKTEST"}

    def on_mount(self) -> None:
        self._page = 1
        self._show_page()
        self.query_one("#cfg-key", Input).focus()

    def _show_page(self) -> None:
        """All widgets stay mounted (save reads them by id); paging only
        toggles a display:none class per panel group."""
        for panel in self.query(".cfg-pg"):
            classes = panel.classes
            on = f"cfg-page-{self._page}" in classes
            if on:
                panel.remove_class("cfg-page-off")
            else:
                panel.add_class("cfg-page-off")
        strip = Text()
        ordered = sorted(self.PAGE_TITLES.items())
        for index, (page, title) in enumerate(ordered):
            if index:
                strip.append(" ")
            strip.append(f"  {title}  ",
                         style="bold reverse" if page == self._page
                         else "color(160)")
        self.query_one("#cfg-pages", Static).update(strip)

    def action_prev_page(self) -> None:
        self._page = (self._page - 2) % len(self.PAGE_TITLES) + 1
        self._show_page()

    def action_next_page(self) -> None:
        self._page = self._page % len(self.PAGE_TITLES) + 1
        self._show_page()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cfg-save":
            self.action_save()
        elif event.button.id == "cfg-cancel":
            self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_save(self) -> None:
        try:
            fast = int(self.query_one("#cfg-fast", Input).value.strip())
            slow = int(self.query_one("#cfg-slow", Input).value.strip())
            lookback = int(self.query_one("#cfg-lookback", Input).value.strip())
            horizon = int(self.query_one("#cfg-kronos-horizon", Input).value.strip())
            intake_timeout = int(self.query_one("#cfg-intake-timeout", Input).value.strip())
            chart_port = int(self.query_one("#cfg-chart-port", Input).value.strip() or "0")
        except ValueError:
            self.notify(
                "Fast/Slow/Lookback/Horizon/Timeout/Chart Port must be integers.",
                severity="error",
            )
            return
        if intake_timeout < 1:
            self.notify("Daily-data timeout must be positive.", severity="error")
            return
        if not 0 <= chart_port <= 65535:
            self.notify("Chart port must be 0 (auto) or 1-65535.", severity="error")
            return
        intake_builder = self.query_one("#cfg-intake-builder", Input).value.strip().lower()
        if intake_builder == "deepseek":
            intake_builder = "ai"  # legacy alias for the LLM editorial pass
        if intake_builder not in {"ai", "deterministic"}:
            self.notify(
                "Report builder must be 'ai' or 'deterministic'.", severity="error"
            )
            return

        data_dir = self.query_one("#cfg-data-dir", Input).value.strip()
        if data_dir:
            # Probe before saving: an unusable path must fail the save, not
            # kill the app from the hot-apply mkdir below.
            try:
                Path(data_dir).mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.notify(f"Data dir is not usable: {exc}", severity="error")
                return

        timezone_name = self.query_one("#cfg-timezone", Input).value.strip()
        if timezone_name:
            try:
                ZoneInfo(timezone_name)
            except (ZoneInfoNotFoundError, ValueError):
                self.notify(f"Unknown timezone: {timezone_name}", severity="error")
                return

        # Remember the final API key under the profile the Base URL points
        # at, so the provider dropdown restores it on the next switch.
        self._stash_current_key()

        try:
            self.config.update({
                "ai_api_key": self.query_one("#cfg-key", Input).value.strip(),
                "ai_base_url": self.query_one("#cfg-base-url", Input).value.strip() or self.config.ai_base_url,
                "ai_model": self.query_one("#cfg-model", Input).value.strip() or self.config.ai_model,
                "ai_profile_keys": self._profile_keys,
                "state_root": self.query_one("#cfg-state-root", Input).value.strip(),
                "data_dir": data_dir,
                "chart_port": chart_port,
                "display_timezone": timezone_name,
                "polymarket_proxy_url": self.query_one("#cfg-polymarket-proxy", Input).value.strip(),
                "eia_api_key": self.query_one("#cfg-eia-key", Input).value.strip(),
                "energy_proxy_url": self.query_one("#cfg-energy-proxy", Input).value.strip(),
                "intake_cmd": self.query_one("#cfg-intake-cmd", Input).value.strip(),
                "workspace_root": self.query_one("#cfg-workspace-root", Input).value.strip()
                                  or self.config.workspace_root,
                "intake_timeout_seconds": intake_timeout,
                "intake_report_builder": intake_builder,
                "default_strategy": self.query_one("#cfg-strategy", Input).value.strip() or self.config.default_strategy,
                "default_fast": fast,
                "default_slow": slow,
                "default_cost_tier": self.query_one("#cfg-cost-tier", Input).value.strip() or self.config.default_cost_tier,
                "default_rebalance": self.query_one("#cfg-rebalance", Input).value.strip() or self.config.default_rebalance,
                "default_lookback": lookback,
                "kronos_cmd": self.query_one("#cfg-kronos-cmd", Input).value.strip(),
                "kronos_horizon": horizon,
                "futures_symbols": [
                    part.upper()
                    for part in self.query_one("#cfg-futures-symbols", Input)
                    .value.replace(",", " ").split()
                    if part.strip()
                ],
                "security_symbols": [
                    part.upper()
                    for part in self.query_one("#cfg-security-symbols", Input)
                    .value.replace(",", " ").split()
                    if part.strip()
                ],
                "futures_cmd": self.query_one("#cfg-futures-cmd", Input).value.strip(),
                "shipping_cmd": self.query_one("#cfg-shipping-cmd", Input).value.strip(),
            })
        except OSError as exc:
            self.notify(f"Config could not be saved: {exc}", severity="error")
            return

        # Hot-apply to the live drivers so no restart is needed.
        self.ai.api_key = self.config.ai_api_key
        self.ai.base_url = self.config.ai_base_url
        self.ai.model = self.config.ai_model
        if self.config.state_root and not getattr(
            self.app, "state_root_override", None
        ):
            self.driver.state_root = self.config.state_root
        if self.config.data_dir:
            self.engine.data_dir = Path(self.config.data_dir)

        self.notify(f"Config saved: {self.config.path}", severity="information")
        self.app.pop_screen()
