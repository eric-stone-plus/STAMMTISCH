"""Command palette provider — keyboard-first jumps across the workstation.

ctrl+p opens the palette anywhere: screen navigation, the quant
workbenches, and offline symbol lookup (multi-market resolve) that
opens the in-terminal chart for the picked ticker.
"""

from __future__ import annotations

from typing import Any, Callable

from textual.command import DiscoveryHit, Hit, Hits, Provider

from .screens.dashboard import DashboardScreen


class WorkstationCommands(Provider):
    """Palette commands: navigation, quant screens, symbol lookup."""

    def _dashboard(self) -> DashboardScreen | None:
        for screen in self.app.screen_stack:
            if isinstance(screen, DashboardScreen):
                return screen
        return None

    def _commands(self) -> list[tuple[str, str, Callable[[], Any]]]:
        dashboard = self._dashboard()
        if dashboard is None:
            return []
        engine, config = dashboard.engine, dashboard.config
        app = dashboard.app

        from .analysis import (
            BacktestScreen, DataFetchScreen, GatesScreen, IndicatorsScreen,
            PortfolioScreen,
        )

        def toggle_language() -> None:
            config.set("language", "zh" if dashboard._language == "en" else "en")
            dashboard._relabel()

        return [
            ("Ask GALAHAD", "AI chat workbench", dashboard.action_open_chat),
            ("Edit config", "Workstation configuration", dashboard.action_edit_config),
            ("Crawlers panel", "Crawl stack operations", dashboard.action_open_crawlers),
            ("Feeds health", "Provider health for the data chains", dashboard.action_open_feeds),
            ("Brokers", "Sandbox execution panel (paper/testnet)", dashboard.action_open_broker),
            ("Trade ledger", "FIFO positions from logged fills", dashboard.action_open_ledger),
            ("Data fetch", "Fetch OHLCV for a symbol", dashboard.action_fetch_data),
            ("Backtest", "Run a strategy backtest",
             lambda: app.push_screen(BacktestScreen(engine, config))),
            ("Indicators", "Technical indicator snapshot",
             lambda: app.push_screen(IndicatorsScreen(engine, config))),
            ("Portfolio", "Portfolio strategy run",
             lambda: app.push_screen(PortfolioScreen(engine, config))),
            ("Gates", "Six-gate evaluation",
             lambda: app.push_screen(GatesScreen(engine, config))),
            ("Language", "Toggle English / 简体中文 chrome", toggle_language),
        ]

    def _symbol_opener(self, symbol: str) -> Callable[[], None]:
        def _open() -> None:
            from .charts import TerminalChartScreen

            dashboard = self._dashboard()
            if dashboard is None:
                return
            dashboard.app.push_screen(
                TerminalChartScreen(dashboard.engine, dashboard.config, symbol))
        return _open

    async def discover(self) -> Hits:
        for name, help_text, callback in self._commands():
            yield DiscoveryHit(name, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, help_text, callback in self._commands():
            if (match := matcher.match(name)) > 0:
                yield Hit(match, matcher.highlight(name), callback, help=help_text)
        # Symbol lookup runs entirely offline through the multi-market
        # resolver: known names, suffixed codes, and bare-digit codes.
        text = query.strip().upper()
        if not text or " " in text:
            return
        from .symbols import resolve_query

        for row in resolve_query(text)[:6]:
            symbol = str(row.get("symbol") or "")
            if not symbol:
                continue
            label = f"{symbol} — {row.get('name') or row.get('market', '')}"
            callback = self._symbol_opener(symbol)
            match = matcher.match(label)
            if match > 0:
                yield Hit(match, matcher.highlight(label), callback,
                          help="Open the in-terminal chart")
            elif symbol == text:
                # Exact-ticker typing should always offer the jump even
                # when the fuzzy matcher scores the label zero.
                yield Hit(1.0, label, callback,
                          help="Open the in-terminal chart")
