"""nmtui-style widgets — clean, minimal, functional."""

from __future__ import annotations

from typing import Any

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static
from textual.widget import Widget

# ── Colors ─────────────────────────────────────────────────────────
WHITE = "#ffffff"
GRAY = "#a0a0a0"
DIM = "#606060"
GREEN = "#66bb6a"
RED = "#ef5350"
AMBER = "#ffd54f"
CYAN = "#4fc3f7"
BG_HEADER = "#303030"


def status_badge(state: str) -> Text:
    styles = {
        "completed": ("black", GREEN), "running": ("black", CYAN),
        "blocked": ("black", AMBER), "failed": ("black", RED),
        "halted": ("black", RED), "staged": ("black", CYAN),
        "created": ("black", CYAN), "unknown": ("black", DIM),
    }
    fg, bg = styles.get(state, styles["unknown"])
    return Text(f" {state.upper()} ", style=f"{fg} on {bg}")


# ═══════════════════════════════════════════════════════════════════
# Stage Flow
# ═══════════════════════════════════════════════════════════════════


class StageFlowWidget(Widget):
    DEFAULT_CSS = "StageFlowWidget { height: auto; min-height: 8; }"

    stages: reactive[list[dict[str, Any]]] = reactive(list)
    active_stage: reactive[str | None] = reactive(None)
    stage_states: reactive[dict[str, str]] = reactive(dict)

    ICONS = {"doctrine": "DOC", "highball": "HBX"}
    COLORS = {"idle": DIM, "running": CYAN, "completed": GREEN, "failed": RED, "blocked": AMBER, "halted": RED}

    def render(self) -> Text:
        if not self.stages:
            return Text("  (no pipeline loaded)", style=DIM)

        out = Text()
        for i, stage in enumerate(self.stages):
            sid = stage.get("id", "?")
            product = stage.get("product", "?")
            gate = stage.get("gate")
            state = self.stage_states.get(sid, "idle")
            color = self.COLORS.get(state, DIM)
            icon = self.ICONS.get(product, "???")

            if i > 0:
                out.append("      │\n", style=DIM if state != "completed" else GREEN)
                out.append("      ▼\n", style=DIM if state != "completed" else GREEN)

            out.append(f"  [", style=DIM)
            out.append(f"{state.upper():>9}", style=color)
            out.append(f"] ", style=DIM)
            out.append(f"{icon}", style=f"bold {color}")
            out.append(f" ─ {product}\n", style=GRAY)

            if gate:
                gs = GREEN if state == "completed" else (RED if state in ("failed", "blocked", "halted") else DIM)
                gsym = "PASS" if state == "completed" else ("FAIL" if state in ("failed", "blocked", "halted") else "----")
                out.append(f"          [{gsym}] {gate}\n", style=gs)

            ins = stage.get("in", [])
            outs = stage.get("out", [])
            if ins or outs:
                parts = []
                if ins:
                    parts.append(f"← {', '.join(ins)}")
                if outs:
                    parts.append(f"→ {', '.join(outs)}")
                out.append(f"          {' | '.join(parts)}\n", style=DIM)

        return out


# ═══════════════════════════════════════════════════════════════════
# Event Timeline
# ═══════════════════════════════════════════════════════════════════


class EventTimeline(Widget):
    DEFAULT_CSS = "EventTimeline { height: auto; min-height: 3; }"

    events: reactive[list[dict[str, Any]]] = reactive(list)

    COLORS = {
        "run.created": CYAN, "run.staged": CYAN, "run.completed": GREEN,
        "run.failed": RED, "run.blocked": AMBER, "run.halted": RED,
        "stage.started": CYAN, "stage.completed": GREEN, "stage.failed": RED,
        "stage.gate_passed": GREEN, "stage.gate_failed": RED,
        "stage.artifact_recorded": DIM, "stage.receipt_accepted": DIM,
    }

    def render(self) -> Text:
        if not self.events:
            return Text("  (no events)", style=DIM)

        t = Text()
        for evt in self.events:
            seq = evt.get("seq", "?")
            etype = evt.get("type", "?")
            at = evt.get("at", "")
            stage = evt.get("stage", "")
            payload = evt.get("payload", {})
            color = self.COLORS.get(etype, DIM)

            ts = str(at).split("T")[1][:12] if "T" in str(at) else str(at)[:12]
            t.append(f"  #{seq:>3} ", style=DIM)
            t.append(f"{ts} ", style=DIM)
            t.append(f"{etype}", style=f"bold {color}")
            if stage:
                t.append(f" [{stage}]", style=DIM)

            detail = payload.get("detail", "")
            gate_id = payload.get("gate_id", "")
            if gate_id:
                t.append(f" gate={gate_id}", style=CYAN)
            if detail:
                t.append(f"  {str(detail)[:80]}", style=DIM)
            t.append("\n")
        return t


# ═══════════════════════════════════════════════════════════════════
# Gate Card
# ═══════════════════════════════════════════════════════════════════


class GateCard(Widget):
    DEFAULT_CSS = """
    GateCard {
        height: auto;
        min-height: 3;
        background: #000000;
        border: solid #505050;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    """

    gate: reactive[dict[str, Any] | None] = reactive(None)

    def render(self) -> Text:
        g = self.gate
        if not g:
            return Text("  (no gate data)", style=DIM)

        t = Text()
        gate_id = g.get("gate_id", "?")
        decision = g.get("decision", "?")
        kind = g.get("kind", "?")
        detail = g.get("detail", "")
        observed = g.get("observed")
        threshold = g.get("threshold")
        on_fail = g.get("on_fail", "")

        is_pass = decision == "pass"
        c = GREEN if is_pass else RED
        sym = "PASS" if is_pass else "FAIL"

        t.append(f"  [{sym}] ", style=f"bold {c}")
        t.append(f"{gate_id}", style=f"bold {c}")
        t.append(f" ({kind})\n", style=DIM)

        if detail:
            t.append(f"    {detail}\n", style=DIM)
        if observed is not None:
            t.append(f"    observed: {observed}\n", style=c)
        if threshold:
            if isinstance(threshold, dict):
                t.append(f"    threshold: {threshold.get('op', '')} {threshold.get('value', '')}\n", style=DIM)
            else:
                # Scalar thresholds are legal gate-record shapes; render
                # them instead of crashing the inspector.
                t.append(f"    threshold: {threshold}\n", style=DIM)
        t.append(f"    on_fail: {on_fail}\n", style=AMBER if on_fail == "blocked" else RED)
        return t


# ═══════════════════════════════════════════════════════════════════
# Digest Widget
# ═══════════════════════════════════════════════════════════════════


class DigestWidget(Static):
    DEFAULT_CSS = "DigestWidget { height: auto; color: #606060; text-style: dim; }"

    digest: reactive[str] = reactive("")

    def render(self) -> Text:
        if not self.digest:
            return Text("  -- no digest --", style=DIM)
        d = self.digest
        prefix, hex_part = ("sha256:", d[7:]) if d.startswith("sha256:") else ("", d)
        t = Text()
        t.append(prefix, style=CYAN)
        for i in range(0, len(hex_part), 8):
            if i > 0:
                t.append(".")
            t.append(hex_part[i:i + 8], style=DIM)
        return t


# ═══════════════════════════════════════════════════════════════════
# System HUD
# ═══════════════════════════════════════════════════════════════════


class SystemHud(Widget):
    DEFAULT_CSS = "SystemHud { height: auto; }"

    state_root: reactive[str] = reactive("")
    initialized: reactive[bool] = reactive(False)
    lock_status: reactive[str] = reactive("UNKNOWN")
    run_count: reactive[int] = reactive(0)
    version: reactive[str] = reactive("0.1.0")
    ai_status: reactive[str] = reactive("OFFLINE")
    disk_usage: reactive[str] = reactive("")

    def compute_disk_usage(self) -> str:
        """Compute the state-root disk usage figure.

        The walk scales with the state root — callers on the UI thread
        run it through a worker.
        """
        from pathlib import Path
        if not self.state_root:
            return ""
        p = Path(self.state_root)
        if not p.is_dir():
            return ""
        try:
            total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        except OSError:
            return ""
        if total > 1_000_000:
            return f"{total / 1_000_000:.1f}MB"
        if total > 1_000:
            return f"{total / 1_000:.1f}KB"
        return f"{total}B"

    def update_disk_usage(self) -> None:
        """Synchronous convenience wrapper (small state roots only)."""
        self.disk_usage = self.compute_disk_usage()

    def render(self) -> Text:
        t = Text()
        t.append("  System Status\n", style=f"bold {WHITE}")

        def kv(key: str, value: str, color: str = GRAY):
            t.append(f"  {key:>10}: ", style=DIM)
            t.append(f"{value}\n", style=color)

        kv("Root", self.state_root or "(not set)", DIM if not self.state_root else GRAY)
        kv("Status", "ONLINE" if self.initialized else "OFFLINE", GREEN if self.initialized else RED)
        kv("Lock", self.lock_status, GREEN if "NOT" in self.lock_status.upper() else AMBER)
        kv("Runs", str(self.run_count), CYAN)
        kv("Version", f"v{self.version}", DIM)
        kv("GALAHAD", self.ai_status, GREEN if "ONLINE" in self.ai_status else RED)

        if self.disk_usage:
            kv("Disk", self.disk_usage, DIM)
        return t


# ═══════════════════════════════════════════════════════════════════
# Typing Text
# ═══════════════════════════════════════════════════════════════════


class TypingText(Static):
    DEFAULT_CSS = "TypingText { height: auto; color: #a0a0a0; }"

    _full_text: str = ""
    _char_idx: reactive[int] = reactive(0)

    def __init__(self, text: str = "", speed: float = 0.02, **kwargs: Any):
        super().__init__(**kwargs)
        self._full_text = text
        self._speed = speed
        self._timer = None

    def on_mount(self) -> None:
        if self._full_text and self._char_idx < len(self._full_text):
            self._timer = self.set_interval(self._speed, self._tick)

    def set_text(self, text: str, speed: float | None = None) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._full_text = text
        self._char_idx = 0
        if speed is not None:
            self._speed = speed
        self._timer = self.set_interval(self._speed, self._tick)

    def _tick(self) -> None:
        if self._char_idx < len(self._full_text):
            self._char_idx = min(self._char_idx + 3, len(self._full_text))
            self.refresh()
        elif self._timer is not None:
            self._timer.stop()
            self._timer = None

    def render(self) -> Text:
        visible = self._full_text[:self._char_idx]
        t = Text(visible, style=GRAY)
        if self._char_idx < len(self._full_text):
            t.append("█", style=GRAY)
        return t
