"""Crawler control panel — the crawling side of the workstation, in one place.

The panel is host-agnostic: it points at whatever crawling runtime the
operator configures (``crawler_url``, ``crawler_compose_dir``,
``crawler_sources_conf``, ``crawler_heal_cmd`` in the workstation
config). With empty settings the panel stays inert — status shows
"not configured" and every switch refuses with a notice instead of
touching anything.

Switches it owns once configured:

- the crawl stack itself (podman compose up/stop over the configured
  compose directory)
- the nightly self-heal timer (firecrawl-watch.timer, a user unit)
- an immediate heal run (the configured heal command)
- the source roster (the configured sources.conf): enable/disable one
  source at a time by commenting its line

All shell operations run off the UI thread; every action appends its
outcome to the in-panel log. Chrome strings are bilingual via tui.lang.
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

try:
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import ScrollableContainer, Vertical
    from textual.screen import Screen
    from textual.widgets import Footer, OptionList, Static
    from textual.widgets.option_list import Option
except ImportError:  # pragma: no cover - textual is optional for lib use
    CrawlerPanelScreen = None  # type: ignore[misc, assignment]
else:

    WATCH_TIMER = "firecrawl-watch.timer"
    STACK_SERVICES = [
        "api", "playwright-service", "redis", "rabbitmq",
        "nuq-postgres", "gfw-proxy",
    ]

    def probe_endpoint(url: str, timeout: float = 2.5) -> tuple[bool, int]:
        """Cheap liveness probe: (reachable, latency ms)."""
        started = time.monotonic()
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=timeout):
                return True, int((time.monotonic() - started) * 1000)
        except urllib.error.HTTPError:
            return True, int((time.monotonic() - started) * 1000)
        except Exception:
            return False, int((time.monotonic() - started) * 1000)

    def _docker_env() -> dict[str, str]:
        env = dict(os.environ)
        env["DOCKER_HOST"] = f"unix:///run/user/{os.getuid()}/podman/podman.sock"
        return env

    def _run(cmd: list[str], cwd: str | None = None, timeout: float = 90.0) -> tuple[int, str]:
        """Run one control command; returns (returncode, tail of output)."""
        try:
            done = subprocess.run(
                cmd,
                cwd=cwd or None,
                env=_docker_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (done.stdout or "") + (done.stderr or "")
            return done.returncode, output.strip()[-800:]
        except subprocess.TimeoutExpired:
            return 124, "timed out"
        except OSError as exc:
            return 127, str(exc)

    def systemctl(*args: str) -> tuple[int, str]:
        return _run(["systemctl", "--user", *args], timeout=30.0)

    def watch_timer_active() -> bool:
        return systemctl("is-active", WATCH_TIMER)[0] == 0

    def parse_sources(path: str) -> list[dict[str, Any]]:
        """One entry per source line: enabled iff not a comment line.

        Only ``market|class|name|url`` rows are toggleable; annotation
        prose is carried but never switched.
        """
        entries: list[dict[str, Any]] = []
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return entries
        for index, raw in enumerate(lines):
            stripped = raw.strip()
            if not stripped:
                continue
            enabled = not stripped.startswith("#")
            body = stripped.lstrip("#").strip()
            parts = body.split("|")
            toggleable = len(parts) >= 4
            entries.append({
                "index": index,
                "enabled": enabled,
                "market": parts[0] if toggleable else "",
                "name": parts[2] if toggleable else body[:40],
                "url": parts[3] if toggleable else "",
                "toggleable": toggleable,
            })
        return entries

    def toggle_source(entry_index: int, path: str) -> bool:
        """Comment/uncomment one source line in place; True on change."""
        try:
            conf = Path(path)
            lines = conf.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        if entry_index >= len(lines):
            return False
        raw = lines[entry_index]
        stripped = raw.strip()
        body = stripped.lstrip("#").strip()
        if not body or len(body.split("|")) < 4:
            return False  # annotation prose is never a source
        if stripped.startswith("#"):
            lines[entry_index] = body
        else:
            indent = raw[: len(raw) - len(raw.lstrip())]
            lines[entry_index] = f"{indent}#{body}"
        conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True

    class CrawlerPanelScreen(Screen):
        """Status + switches for the whole crawling side."""

        BINDINGS = [
            Binding("escape", "back", "Back"),
            Binding("p", "refresh", "Refresh"),
            Binding("s", "toggle_stack", "Stack on/off"),
            Binding("t", "toggle_timer", "Timer on/off"),
            Binding("r", "restart_api", "Restart api"),
            Binding("h", "heal_now", "Heal now"),
            Binding("e", "toggle_source", "Enable/disable"),
        ]
        CSS = """
        CrawlerPanelScreen { layout: vertical; }
        #crawl-scroll { height: 1fr; }
        .panel { border: solid #505050; margin: 0 1; }
        .panel-title { color: #a0a0a0; }
        #crawl-sources { height: auto; max-height: 14; }
        #crawl-log { height: auto; max-height: 8; color: #a0a0a0; }
        """

        def __init__(self, config: Any = None, **kwargs: Any):
            super().__init__(**kwargs)
            self.config = config
            self._log_lines: list[str] = []

        def _cfg(self, key: str, default: str = "") -> str:
            try:
                value = self.config.get(key, default)
            except Exception:
                value = default
            return str(value or default)

        @property
        def _language(self) -> str:
            return "zh" if self._cfg("language", "en") == "zh" else "en"

        def _tr(self, key: str, fallback: str) -> str:
            from .lang import tr

            return tr(self._language, key, fallback)

        def compose(self) -> ComposeResult:
            yield Static(
                "  CRAWLERS  |  [S]stack [T]timer [R]restart api [H]heal "
                "[E]toggle source [P]refresh  [Esc] Back",
                classes="header-bar",
            )
            with ScrollableContainer(id="crawl-scroll"):
                with Vertical(classes="panel"):
                    yield Static(
                        "  " + self._tr("crawlers.status", "Status"),
                        classes="panel-title",
                    )
                    yield Static("  probing...\n", id="crawl-status")
                with Vertical(classes="panel"):
                    yield Static(
                        "  " + self._tr("crawlers.sources", "Sources"),
                        classes="panel-title",
                    )
                    yield OptionList(id="crawl-sources")
                with Vertical(classes="panel"):
                    yield Static("  " + self._tr("crawlers.log", "Log"), classes="panel-title")
                    yield Static(
                        "  " + self._tr("crawlers.no_ops", "(no operations yet)") + "\n",
                        id="crawl-log",
                    )
            yield Footer()

        def on_mount(self) -> None:
            self.action_refresh()

        # -- status ------------------------------------------------------------

        def _snapshot(self) -> dict[str, Any]:
            endpoint = self._cfg("crawler_url", "http://127.0.0.1:3002/")
            endpoint_ok, latency_ms = probe_endpoint(endpoint)
            return {
                "endpoint": endpoint,
                "endpoint_ok": endpoint_ok,
                "latency_ms": latency_ms,
                "timer_active": watch_timer_active(),
                "compose_dir": self._cfg("crawler_compose_dir"),
                "sources_conf": self._cfg("crawler_sources_conf"),
                "heal_cmd": self._cfg("crawler_heal_cmd"),
                "intake_configured": bool(
                    self.config and self.config.intake_argv
                ),
            }

        def _status_text(self, snap: dict[str, Any]) -> str:
            up = self._tr("crawlers.up", "UP")
            down = self._tr("crawlers.down", "DOWN")
            on = self._tr("crawlers.on", "ON")
            off = self._tr("crawlers.off", "OFF")
            yes = self._tr("crawlers.configured", "configured")
            no = self._tr("crawlers.not_configured", "not configured")
            endpoint = (
                f"{up} ({snap['latency_ms']}ms)" if snap["endpoint_ok"] else down
            )
            timer = on if snap["timer_active"] else off
            lines = [
                f"  {self._tr('crawlers.endpoint', 'Firecrawl endpoint')}   {endpoint}   {snap['endpoint']}",
                f"  {self._tr('crawlers.timer', 'Self-heal timer')}       {timer}",
                f"  {self._tr('crawlers.intake', 'Intake command')}        {yes if snap['intake_configured'] else no}",
            ]
            for label, value in (
                ("crawler_compose_dir", snap["compose_dir"]),
                ("crawler_sources_conf", snap["sources_conf"]),
                ("crawler_heal_cmd", snap["heal_cmd"]),
            ):
                lines.append(f"  {label:<18} {value if value else no}")
            return "\n".join(lines)

        def action_refresh(self) -> None:
            from .analysis import _run_async

            def _work():
                return self._snapshot()

            def _apply(snap):
                self.query_one("#crawl-status", Static).update(
                    self._status_text(snap) + "\n"
                )
                self._reload_sources()

            _run_async(self, _work, _apply)

        def _reload_sources(self) -> None:
            listing = self.query_one("#crawl-sources", OptionList)
            listing.clear()
            conf = self._cfg("crawler_sources_conf")
            if not conf:
                listing.add_option(
                    Option("  (crawler_sources_conf not set)", id="none")
                )
                return
            for entry in parse_sources(conf):
                if not entry["toggleable"]:
                    continue
                mark = "+" if entry["enabled"] else "-"
                listing.add_option(
                    Option(
                        f"  {mark} {entry['market']:<10} {entry['name']:<18} {entry['url'][:44]}",
                        id=str(entry["index"]),
                    )
                )

        # -- log ---------------------------------------------------------------

        def _log(self, line: str) -> None:
            stamp = time.strftime("%H:%M:%S")
            self._log_lines.append(f"  {stamp}  {line}")
            self._log_lines = self._log_lines[-8:]
            self.query_one("#crawl-log", Static).update(
                "\n".join(self._log_lines) + "\n"
            )

        # -- actions ------------------------------------------------------------

        def _run_op(self, label: str, work) -> None:
            from .analysis import _run_async

            def _deliver(result):
                code, output = result
                self._log(f"{label}: {'OK' if code == 0 else f'exit {code}'}  {output[-200:]}")
                self.action_refresh()

            _run_async(self, work, _deliver)

        def action_toggle_stack(self) -> None:
            compose_dir = self._cfg("crawler_compose_dir")
            if not compose_dir:
                self.notify("crawler_compose_dir is not configured.", severity="warning")
                return
            up = probe_endpoint(self._cfg("crawler_url", "http://127.0.0.1:3002/"))[0]

            def _work():
                if up:
                    return _run(
                        ["podman", "compose", "stop", *STACK_SERVICES],
                        cwd=compose_dir,
                    )
                return _run(
                    ["podman", "compose", "up", "-d", "--no-deps", *STACK_SERVICES],
                    cwd=compose_dir,
                    timeout=180.0,
                )

            self._run_op("stack stop" if up else "stack start", _work)

        def action_toggle_timer(self) -> None:
            active = watch_timer_active()

            def _work():
                if active:
                    return systemctl("disable", "--now", WATCH_TIMER)
                return systemctl("enable", "--now", WATCH_TIMER)

            self._run_op("timer off" if active else "timer on", _work)

        def action_restart_api(self) -> None:
            if not self._cfg("crawler_compose_dir"):
                self.notify("crawler_compose_dir is not configured.", severity="warning")
                return

            def _work():
                return _run(["podman", "restart", "firecrawl-api-1"], timeout=120.0)

            self._run_op("restart api", _work)

        def action_heal_now(self) -> None:
            heal_cmd = self._cfg("crawler_heal_cmd")
            if not heal_cmd:
                self.notify("crawler_heal_cmd is not configured.", severity="warning")
                return

            def _work():
                return _run(["bash", heal_cmd], timeout=300.0)

            self._run_op("heal", _work)

        def action_toggle_source(self) -> None:
            conf = self._cfg("crawler_sources_conf")
            if not conf:
                self.notify("crawler_sources_conf is not configured.", severity="warning")
                return
            listing = self.query_one("#crawl-sources", OptionList)
            highlighted = listing.highlighted
            if highlighted is None:
                self.notify("Select a source first.", severity="warning")
                return
            option = listing.get_option_at_index(highlighted)
            if str(option.id) == "none":
                return
            changed = toggle_source(int(str(option.id)), conf)
            if changed:
                self._log(f"source toggled: {str(option.prompt).strip()[:70]}")
                self._reload_sources()
            else:
                self.notify("That line is not a toggleable source.", severity="warning")

        def action_back(self) -> None:
            self.app.pop_screen()
