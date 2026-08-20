"""Crawler control panel — the crawling side of the workstation, in one place.

The panel is host-agnostic: it points at whatever crawling runtime the
operator configures (``crawler_url``, ``crawler_compose_dir``,
``crawler_sources_conf``, ``crawler_heal_cmd`` in the workstation
config). With empty settings the panel stays inert — status shows
"not configured" and every switch refuses with a notice instead of
touching anything.

What it operates once configured:

- the crawl stack itself (podman compose up/stop over the configured
  compose directory), plus a single-container api restart
- the nightly self-heal timer (firecrawl-watch.timer, a user unit) and
  an immediate heal run (the configured heal command)
- the source roster (the configured sources.conf): per-source
  enable/disable by commenting its line, with an atomic write

All shell operations run off the UI thread and report both in a log
pane and as notices. Chrome strings are bilingual via tui.lang.
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
    DEFAULT_ENDPOINT = "http://127.0.0.1:3002/"

    def probe_endpoint(
        url: str = DEFAULT_ENDPOINT, timeout: float = 2.5
    ) -> tuple[bool, int]:
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

    def _run(
        cmd: list[str], cwd: str | None = None, timeout: float = 90.0
    ) -> tuple[int, str]:
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

    def watch_timer_next() -> str:
        code, value = systemctl("show", WATCH_TIMER, "--value",
                                "--property=NextElapseUSecRealtime")
        if code != 0:
            return ""
        return value.strip()

    def container_counts(compose_dir: str) -> tuple[int, int]:
        """(running, total) containers of the compose project."""
        if not compose_dir:
            return (0, 0)
        running = 0
        total = 0
        for flag in ("", "-a"):
            cmd = ["podman", "ps", *filter(None, [flag]),
                   "--format", "{{.Names}}"]
            code, output = _run(cmd, cwd=compose_dir, timeout=20.0)
            if code != 0:
                continue
            names = [n for n in output.splitlines() if n.strip()]
            if not flag:
                running = len(names)
            else:
                total = len(names)
        return (running, total)

    def _is_source(body: str) -> bool:
        """A toggleable source row: exactly ``phase|folder|name|url`` with a
        plain http(s) URL. Everything else — including header prose that
        happens to contain four ``|``-separated words — is annotation."""
        parts = body.split("|")
        if len(parts) != 4 or not all(part.strip() for part in parts):
            return False
        url = parts[3].strip()
        return url.startswith(("http://", "https://"))

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
            toggleable = _is_source(body)
            entries.append({
                "index": index,
                "enabled": enabled,
                "market": parts[0].strip() if toggleable else "",
                "name": parts[2].strip() if toggleable else body[:40],
                "url": parts[3].strip() if toggleable else "",
                "toggleable": toggleable,
            })
        return entries

    def toggle_source(entry_index: int, path: str) -> bool:
        """Comment/uncomment one source line atomically; True on change."""
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
        if not _is_source(body):
            return False  # annotation prose is never a source
        if stripped.startswith("#"):
            lines[entry_index] = body
        else:
            indent = raw[: len(raw) - len(raw.lstrip())]
            lines[entry_index] = f"{indent}#{body}"
        tmp = conf.with_name(conf.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, conf)
        return True

    def _fit(value: str, width: int) -> str:
        cleaned = " ".join(str(value or "").split())
        if len(cleaned) <= width:
            return cleaned
        return cleaned[: max(width - 1, 1)] + "…"

    class CrawlerPanelScreen(Screen):
        """Status + switches for the whole crawling side."""

        BINDINGS = [
            Binding("escape", "back", "Back"),
            Binding("p", "refresh", "Refresh"),
            Binding("s", "toggle_stack", "Stack on/off"),
            Binding("t", "toggle_timer", "Timer on/off"),
            Binding("r", "restart_api", "Restart api"),
            Binding("h", "heal_now", "Heal now"),
            Binding("e", "toggle_source", "Toggle source"),
            Binding("question_mark", "show_help", "Keys"),
        ]
        CSS = """
        CrawlerPanelScreen { layout: vertical; }
        #crawl-scroll { height: 1fr; }
        .panel { border: solid #505050; margin: 0 1; }
        .panel-title { color: #a0a0a0; }
        #crawl-sources { height: auto; max-height: 24; }
        """

        def __init__(self, config: Any = None, **kwargs: Any):
            super().__init__(**kwargs)
            self.config = config

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
                "[E]/[Enter] toggle source [P]refresh  [Esc] Back",
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
                        id="crawl-sources-title",
                    )
                    yield OptionList(id="crawl-sources")
            yield Footer()

        def on_mount(self) -> None:
            self.action_refresh()

        # -- status ------------------------------------------------------------

        def _snapshot(self) -> dict[str, Any]:
            endpoint = self._cfg("crawler_url", DEFAULT_ENDPOINT)
            endpoint_ok, latency_ms = probe_endpoint(endpoint)
            compose_dir = self._cfg("crawler_compose_dir")
            running, total = container_counts(compose_dir)
            return {
                "endpoint": endpoint,
                "endpoint_ok": endpoint_ok,
                "latency_ms": latency_ms,
                "timer_active": watch_timer_active(),
                "timer_next": watch_timer_next() if watch_timer_active() else "",
                "containers_running": running,
                "containers_total": total,
                "compose_dir": compose_dir,
                "sources_conf": self._cfg("crawler_sources_conf"),
                "heal_cmd": self._cfg("crawler_heal_cmd"),
                "intake_configured": bool(self.config and self.config.intake_argv),
            }

        def _status_text(self, snap: dict[str, Any]) -> str:
            up = self._tr("crawlers.up", "UP")
            down = self._tr("crawlers.down", "DOWN")
            on = self._tr("crawlers.on", "ON")
            off = self._tr("crawlers.off", "OFF")
            yes = self._tr("crawlers.configured", "configured")
            no = self._tr("crawlers.not_configured", "not configured")
            endpoint = (
                f"{up} {snap['latency_ms']}ms" if snap["endpoint_ok"] else down
            )
            timer = on if snap["timer_active"] else off
            if snap.get("timer_next"):
                timer += f"  next {snap['timer_next']}"
            rows = [
                (
                    self._tr("crawlers.endpoint", "Firecrawl endpoint"),
                    f"{endpoint}  {_fit(snap['endpoint'], 44)}",
                ),
                (
                    self._tr("crawlers.containers", "Containers"),
                    f"{snap['containers_running']}/{snap['containers_total']} "
                    + self._tr("crawlers.running", "running"),
                ),
                (
                    self._tr("crawlers.timer", "Self-heal timer"),
                    timer,
                ),
                (
                    self._tr("crawlers.intake", "Intake command"),
                    yes if snap["intake_configured"] else no,
                ),
                ("compose dir", _fit(snap["compose_dir"], 56) or no),
                ("sources conf", _fit(snap["sources_conf"], 56) or no),
                ("heal command", _fit(snap["heal_cmd"], 56) or no),
            ]
            return "\n".join(f"  {label:<18} {value}" for label, value in rows)

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
            was_highlighted = listing.highlighted
            listing.clear_options()
            conf = self._cfg("crawler_sources_conf")
            entries = [e for e in parse_sources(conf) if e["toggleable"]] if conf else []
            enabled = sum(1 for e in entries if e["enabled"])
            title = self.query_one("#crawl-sources-title", Static)
            if not conf:
                title.update(
                    "  " + self._tr("crawlers.sources", "Sources")
                    + "  —  crawler_sources_conf "
                    + self._tr("crawlers.not_configured", "not configured")
                )
                return
            title.update(
                f"  {self._tr('crawlers.sources', 'Sources')}  —  "
                f"{enabled}/{len(entries)} "
                + self._tr("crawlers.enabled", "enabled")
                + "  ·  [E]/Enter "
                + self._tr("crawlers.toggle_hint", "toggle")
            )
            # Column header, aligned with the rows below it; disabled so it
            # can never be toggled.
            listing.add_option(
                Option(
                    "    PHASE      NAME             URL",
                    id="none",
                    disabled=True,
                )
            )
            for entry in entries:
                mark = "✓" if entry["enabled"] else "✗"
                row = (
                    f"  {mark} {entry['market']:<9} {entry['name']:<16} "
                    f"{_fit(entry['url'], 40)}"
                )
                listing.add_option(Option(row, id=str(entry["index"])))
            if entries:
                listing.highlighted = min(
                    was_highlighted if was_highlighted is not None else 1,
                    len(entries),
                )

        # -- log + ops -----------------------------------------------------------

        def _run_op(self, label: str, work) -> None:
            from .analysis import _run_async

            def _deliver(result):
                code, output = result
                ok = code == 0
                detail = f"  {output[-120:]}" if not ok else ""
                self.notify(
                    f"{label}: {'OK' if ok else f'exit {code}'}{detail}",
                    severity="information" if ok else "error",
                    timeout=8 if ok else 12,
                )
                self.action_refresh()

            _run_async(self, work, _deliver)

        def _require(self, key: str) -> str:
            value = self._cfg(key)
            if not value:
                self.notify(f"{key} is not configured.", severity="warning")
            return value

        def action_toggle_stack(self) -> None:
            compose_dir = self._require("crawler_compose_dir")
            if not compose_dir:
                return
            up = probe_endpoint(self._cfg("crawler_url", DEFAULT_ENDPOINT))[0]

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
            if not self._require("crawler_compose_dir"):
                return

            def _work():
                return _run(["podman", "restart", "firecrawl-api-1"], timeout=120.0)

            self._run_op("restart api", _work)

        def action_heal_now(self) -> None:
            heal_cmd = self._require("crawler_heal_cmd")
            if not heal_cmd:
                return

            def _work():
                return _run(["bash", heal_cmd], timeout=300.0)

            self._run_op("heal", _work)

        # -- sources ---------------------------------------------------------------

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            """Enter on a source row toggles it, matching [E]."""
            if event.option_list.id != "crawl-sources":
                return
            if str(event.option_id) != "none":
                self._toggle_source_line(int(str(event.option_id)))

        def action_toggle_source(self) -> None:
            listing = self.query_one("#crawl-sources", OptionList)
            highlighted = listing.highlighted
            if highlighted is None:
                self.notify("Select a source first.", severity="warning")
                return
            option = listing.get_option_at_index(highlighted)
            if str(option.id) == "none":
                return
            self._toggle_source_line(int(str(option.id)))

        def _toggle_source_line(self, entry_index: int) -> None:
            conf = self._require("crawler_sources_conf")
            if not conf:
                return
            changed = toggle_source(entry_index, conf)
            if changed:
                self.notify(
                    f"source toggled (line {entry_index + 1})",
                    severity="information",
                )
                self._reload_sources()
            else:
                self.notify("That line is not a toggleable source.", severity="warning")

        def action_show_help(self) -> None:
            from .screens import KeyHelpScreen

            self.app.push_screen(KeyHelpScreen("CRAWLERS — KEYS", [
                ("p", self._tr("crawlers.k.refresh", "refresh status + sources")),
                ("s", self._tr("crawlers.k.stack", "crawl stack on/off (compose stop/up)")),
                ("t", self._tr("crawlers.k.timer", "self-heal timer on/off")),
                ("r", self._tr("crawlers.k.restart", "restart the api container")),
                ("h", self._tr("crawlers.k.heal", "run the heal command now")),
                ("e / Enter", self._tr("crawlers.k.toggle", "enable/disable the highlighted source")),
                ("↑ ↓", self._tr("crawlers.k.move", "move in the source list")),
                ("Esc", self._tr("crawlers.k.back", "back to the dashboard")),
            ]))

        def action_back(self) -> None:
            self.app.pop_screen()
