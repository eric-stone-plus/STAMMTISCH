"""Pure snapshot -> Rich renderables for every workstation panel.

No Textual import: the same builders feed the TUI screens (tier 3) and the
Rich Live rotator (tier 2). Colors resolve ONLY through
:mod:`interface.render.tokens` (``token()`` / ``STATE_TOKENS``) — never a
hex literal, never a guessed color. Never-fake-data: a missing cost renders
an em dash, a missing gate count renders an em dash, never ``0``.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from interface.render.flags import letter_flags
from interface.render.tokens import STATE_TOKENS, token
from interface.snapshot import (
    QUOTE_STALE_S,
    CostSnapshot,
    Event,
    QuoteSnapshot,
    RunSnapshot,
    ServiceStatus,
    WorkstationSnapshot,
    run_flags,
    workstation_flags,
)

__all__ = [
    "RUNS_COLUMNS",
    "banner",
    "cost_block",
    "feed_line",
    "followed_run",
    "gates_table_with_records",
    "glance_block",
    "run_detail",
    "run_events_body",
    "run_summary_line",
    "runs_detail_body",
    "runs_table",
    "runs_table_rows",
    "services_strip",
    "stages_flow",
]

#: Column keys of the runs table (Textual DataTable column keys and the
#: rich Table headers share this vocabulary).
RUNS_COLUMNS: tuple[str, ...] = ("run", "pipeline", "state", "flags", "gates", "cost")

#: States that make a run the default feed source when nobody follows
#: explicitly. ``resumed`` is deliberately absent (grilling D3): a
#: possibly-dead-again host must not be auto-chased; the explicit ``f``
#: follow verb (M3) is the way to watch one.
_ACTIVE_RUN_STATES = frozenset({"created", "staged", "running", "gating"})

_MISSING = "—"


def _bare_table() -> Table:
    """The shared borderless compact table (one shape, three call sites)."""
    return Table(box=None, pad_edge=False, show_edge=False)


def _state_style(state: str) -> str:
    return token(STATE_TOKENS.get(state, "state.unknown"))


def _fmt_age(age_s: float) -> str:
    if age_s < 90.0:
        return f"{age_s:.0f}s"
    if age_s < 5_400.0:
        return f"{age_s / 60:.0f}m"
    return f"{age_s / 3_600:.1f}h"


def followed_run(
    snapshot: WorkstationSnapshot, *, explicit_id: str | None = None
) -> RunSnapshot | None:
    """The run whose event tail feeds the activity surface.

    An explicit ``f``-follow target overrides the auto policy whenever
    the run is still in the frame (grilling D3 pin); when the target is
    gone the auto policy resumes. Auto policy: the first run in an
    active state, else the first run, else ``None``. Pure presentation
    policy, shared by the TUI feed and the watch rotator.
    """
    if explicit_id is not None:
        for run in snapshot.runs:
            if run.id == explicit_id:
                return run
    for run in snapshot.runs:
        if run.state in _ACTIVE_RUN_STATES:
            return run
    return snapshot.runs[0] if snapshot.runs else None


def _gates_cell(run: RunSnapshot) -> Text:
    if not run.gates:
        return Text(_MISSING, style=token("text.muted"))
    passed = sum(1 for gate in run.gates if gate.decision == "PASS")
    failed = len(run.gates) - passed
    style = token("state.crit") if failed else token("state.ok")
    marks = "!" * min(failed, 3)
    return Text(f"{passed}/{len(run.gates)}{marks}", style=style)


def _cost_cell(run: RunSnapshot) -> Text:
    if run.cost is None:
        return Text(_MISSING, style=token("text.muted"))
    return Text(f"{run.cost.total:.4f}", style=token("cost.total"))


def runs_table_rows(
    snapshot: WorkstationSnapshot,
    runs: Sequence[RunSnapshot] | None = None,
) -> list[list[Text]]:
    """One list of cell renderables per run, aligned with ``runs``.

    ``runs`` defaults to ``snapshot.runs``; the overview passes the
    view-model's ordered+filtered projection instead (sort/filter are
    screen-side concerns; the cells stay identical).

    The flag cell derives workstation-level ``F``/``D`` once (stale feed,
    degraded services) and applies it to every run row, mirroring
    ``workstation_flags`` semantics at row level via ``run_flags``.
    """
    flags = workstation_flags(snapshot)
    rows: list[list[Text]] = []
    for run in snapshot.runs if runs is None else runs:
        rows.append(
            [
                Text(run.id, style=token("text.primary")),
                Text(run.pipeline_id, style=token("text.muted")),
                Text(run.state, style=_state_style(run.state)),
                letter_flags(run_flags(run, feed_stale=flags["F"], degraded=flags["D"])),
                _gates_cell(run),
                _cost_cell(run),
            ]
        )
    return rows


def runs_table(snapshot: WorkstationSnapshot) -> Table:
    """The runs wall as one rich Table (tier 2 pages; the TUI uses rows)."""
    table = _bare_table()
    table.caption_style = token("text.muted")
    for column in RUNS_COLUMNS:
        table.add_column(
            column.upper(),
            style=token("text.muted"),
            justify="right" if column in ("gates", "cost") else "left",
        )
    for cells in runs_table_rows(snapshot):
        table.add_row(*cells)
    return table


def feed_line(event: Event) -> Text:
    """One activity-feed line: ``ts  kind  summary`` colored by event kind."""
    line = Text()
    line.append(f"{event.ts} ", style=token("feed.ts"))
    line.append(f"{event.kind:<26}", style=_kind_style(event.kind))
    line.append(event.summary, style=token("text.primary"))
    return line


def _kind_style(kind: str) -> str:
    if kind.startswith("run."):
        return token("feed.kind_run")
    if kind == "stage.gate_passed":
        return token("feed.kind_gate_pass")
    if kind == "stage.gate_failed":
        return token("feed.kind_gate_fail")
    if kind.startswith("stage."):
        return token("feed.kind_stage")
    if kind.startswith("intake."):
        return token("feed.kind_intake")
    return token("text.primary")


def glance_block(quotes: Iterable[QuoteSnapshot]) -> Text:
    """The glance panel body: one line per quote with age + STALE + src."""
    block = Text()
    for index, quote in enumerate(quotes):
        if index:
            block.append("\n")
        stale = quote.age_s > QUOTE_STALE_S
        block.append(f"{quote.label:<8}", style=token("text.primary"))
        block.append(f" {quote.last:>11,.2f}", style=token("panel.title"))
        change_style = token("market.up" if quote.change_pct >= 0 else "market.down")
        block.append(f" {quote.change_pct:+5.2f}%", style=change_style)
        block.append(f" {_fmt_age(quote.age_s):>5}", style=token("text.muted"))
        if stale:
            block.append(" STALE", style=f"bold {token('stale.crit')}")
        block.append(f" src {quote.source}", style=token("provenance.src"))
    if not len(block):
        return Text("(no quotes)", style=token("text.muted"))
    return block


def services_strip(services: Iterable[ServiceStatus]) -> Text:
    """The services strip: ``name ok/DOWN`` pairs joined with middots."""
    strip = Text()
    for index, service in enumerate(services):
        if index:
            strip.append(" · ", style=token("text.muted"))
        strip.append(service.name, style=token("text.muted"))
        state = "ok" if service.available else "DOWN"
        style = token("state.ok") if service.available else token("state.crit")
        detail = f" ({service.detail})" if service.detail else ""
        strip.append(f" {state}{detail}", style=style)
    if not len(strip):
        return Text("(no services)", style=token("text.muted"))
    return strip


def banner(snapshot: WorkstationSnapshot) -> Text:
    """The workstation banner: title, flag cell, local stamp, error mark."""
    line = Text()
    line.append("STAMMTISCH ", style=f"bold {token('panel.title')}")
    line.append(letter_flags(workstation_flags(snapshot)))
    stamp = time.strftime("%H:%M:%S", time.localtime(snapshot.taken_at))
    line.append(f"  {stamp}", style=token("text.muted"))
    if snapshot.collector_error:
        line.append("  COLLECTOR ERROR", style=f"bold {token('state.crit')}")
    return line


def _stages_table(run: RunSnapshot) -> Table:
    table = _bare_table()
    for column in ("stage", "product", "state", "gate"):
        table.add_column(column, style=token("text.muted"))
    if not run.stages:
        table.add_row(Text(_MISSING, style=token("text.muted")))
    for stage in run.stages:
        table.add_row(
            Text(stage.id, style=token("text.primary")),
            Text(stage.product, style=token("text.muted")),
            Text(stage.state, style=_state_style(stage.state)),
            Text(stage.gate or _MISSING, style=token("text.muted")),
        )
    return table


def _gates_table(run: RunSnapshot) -> Table:
    table = _bare_table()
    for column in ("gate", "stage", "decision", "evaluated"):
        table.add_column(column, style=token("text.muted"))
    if not run.gates:
        table.add_row(Text(_MISSING, style=token("text.muted")))
    for gate in run.gates:
        if gate.decision == "PASS":
            decision = Text(gate.decision, style=token("state.ok"))
        elif gate.decision == "FAIL":
            decision = Text(gate.decision, style=token("state.crit"))
        else:
            decision = Text(gate.decision, style=token("state.warn"))
        table.add_row(
            Text(gate.gate_id, style=token("text.primary")),
            Text(gate.stage, style=token("text.muted")),
            decision,
            Text(gate.evaluated_at, style=token("text.muted")),
        )
    return table


def _cost_line(run: RunSnapshot) -> Text:
    if run.cost is None:
        return Text(f"cost  {_MISSING}", style=token("text.muted"))
    by_stage = "  ".join(
        f"{stage} {amount:.4f}" for stage, amount in sorted(run.cost.by_stage.items())
    )
    suffix = f" ({by_stage})" if by_stage else ""
    line = Text("cost ", style=token("text.muted"))
    line.append(f"{run.cost.total:.4f} {run.cost.currency}", style=token("cost.total"))
    line.append(suffix, style=token("text.muted"))
    return line


def run_detail(run: RunSnapshot) -> RenderableType:
    """The full run inspector: header, stages, gates, cost, full event tail."""
    header = Text()
    header.append(run.id, style=f"bold {token('panel.title')}")
    header.append(
        f"  {run.pipeline_id}  ", style=token("text.muted")
    )
    header.append(run.state, style=f"bold {_state_style(run.state)}")
    header.append(f"  since {run.created_at}", style=token("text.muted"))
    parts: list[RenderableType] = [header]
    if run.error:
        parts.append(Text(f"error: {run.error}", style=token("state.crit")))
    parts.append(Text("stages", style=f"bold {token('accent')}"))
    parts.append(_stages_table(run))
    parts.append(Text("gates", style=f"bold {token('accent')}"))
    parts.append(_gates_table(run))
    parts.append(_cost_line(run))
    parts.append(Text("events", style=f"bold {token('accent')}"))
    tail = _event_tail(run.events_tail)
    if tail is None:
        parts.append(Text("(no events)", style=token("text.muted")))
    else:
        parts.append(tail)
    return Group(*parts)


def _event_tail(events: Sequence[Event]) -> RenderableType | None:
    if not events:
        return None
    return Group(*(feed_line(event) for event in events))


# ── M3 additions: verb surfaces + the full detail body ──────────────────


def run_summary_line(run: RunSnapshot) -> str:
    """The ``c`` copy verb's one-line masked summary (snapshot fields only).

    Masking policy: free-text ``run.error`` is NOT copied — collector
    errors embed host paths; a bare marker carries the fact instead.
    Token totals carry the honest unit; unknown renders the em dash.
    """
    gates = (
        f"{sum(1 for g in run.gates if g.decision == 'PASS')}/{len(run.gates)}"
        if run.gates else _MISSING
    )
    tokens = (
        f"{run.cost.token_total:,} {run.cost.unit}"
        if run.cost is not None and run.cost.token_total is not None
        else _MISSING
    )
    error = " [error]" if run.error else ""
    return (f"run {run.id} {run.pipeline_id} {run.state} "
            f"gates {gates} tokens {tokens}{error}")


def run_events_body(run: RunSnapshot) -> RenderableType:
    """The ``v`` events modal body: the FULL tail of one run, uncapped."""
    if not run.events_tail:
        return Text("(no events)", style=token("text.muted"))
    return Group(*(feed_line(event) for event in run.events_tail))


def _num(value: float | None, spec: str) -> Text:
    """A numeric cell that never invents a value for a legal null."""
    if value is None:
        return Text(_MISSING, style=token("text.muted"))
    return Text(format(value, spec), style=token("text.primary"))


def cost_block(cost: CostSnapshot | None) -> RenderableType:
    """The honest cost block: unit, token_total, wall_s, per-stage usage.

    cost-ledger v0 numbers are token counts and wall seconds (grilling
    adjudication 1) — never money; every schema-legal null renders an em
    dash, never an invented 0.
    """
    if cost is None:
        return Text(f"cost  {_MISSING}", style=token("text.muted"))
    head = Text("cost ", style=token("text.muted"))
    head.append(f"unit {cost.unit}", style=token("cost.total"))
    head.append("  tokens ", style=token("text.muted"))
    head.append(f"{cost.token_total:,}" if cost.token_total is not None
                else _MISSING, style=token("cost.total"))
    head.append("  wall ", style=token("text.muted"))
    head.append(f"{cost.wall_s:.1f}s" if cost.wall_s is not None else _MISSING,
                style=token("text.primary"))
    if cost.generated_at:
        head.append(f"  at {cost.generated_at}", style=token("text.muted"))
    parts: list[RenderableType] = [head]
    if cost.usage:
        table = _bare_table()
        for column in ("stage", "tokens in", "tokens out", "tokens total",
                       "wall s"):
            table.add_column(column, style=token("text.muted"))
        for row in cost.usage:
            table.add_row(
                Text(row.stage, style=token("text.primary")),
                _num(row.tokens_in, ","),
                _num(row.tokens_out, ","),
                _num(row.tokens_total, ","),
                _num(row.wall_s, ".1f"),
            )
        parts.append(table)
    return Group(*parts)


def stages_flow(run: RunSnapshot) -> Text:
    """The stages as one compact flow line: ``ingest[completed] → …``."""
    if not run.stages:
        return Text("(no stages)", style=token("text.muted"))
    flow = Text()
    for index, stage in enumerate(run.stages):
        if index:
            flow.append(" → ", style=token("text.muted"))
        flow.append(stage.id, style=token("text.primary"))
        flow.append(f"[{stage.state}]", style=_state_style(stage.state))
    return flow


def _short_record(record_sha256: str) -> str:
    """Short gate-record digest for humans (full value stays in the data)."""
    hexpart = record_sha256.removeprefix("sha256:")
    return f"{hexpart[:12]}…" if len(hexpart) > 12 else hexpart


def gates_table_with_records(run: RunSnapshot) -> Table:
    """The detail gates table: decision + the gate-RECORD digest.

    ``record_sha256`` is the gate record's own digest (grilling
    adjudication 2); the evaluated artifact's digest stays unread in
    ``gates/<stage>.gate.json`` until that lane exists.
    """
    table = _bare_table()
    for column in ("gate", "stage", "decision", "record", "evaluated"):
        table.add_column(column, style=token("text.muted"))
    if not run.gates:
        table.add_row(Text(_MISSING, style=token("text.muted")))
    for gate in run.gates:
        if gate.decision == "PASS":
            decision = Text(gate.decision, style=token("state.ok"))
        elif gate.decision == "FAIL":
            decision = Text(gate.decision, style=token("state.crit"))
        else:
            decision = Text(gate.decision, style=token("state.warn"))
        table.add_row(
            Text(gate.gate_id, style=token("text.primary")),
            Text(gate.stage, style=token("text.muted")),
            decision,
            Text(_short_record(gate.record_sha256), style=token("text.muted")),
            Text(gate.evaluated_at, style=token("text.muted")),
        )
    return table


def runs_detail_body(run: RunSnapshot) -> RenderableType:
    """The ``:detail`` full-screen body.

    Corrupt-run policy (grilling S4): ``run.error`` renders prominently
    first, and stages/gates render their honest empty placeholders — a
    corrupt run never gets fake stages.
    """
    header = Text()
    header.append(run.id, style=f"bold {token('panel.title')}")
    header.append(f"  {run.pipeline_id}  ", style=token("text.muted"))
    header.append(run.state, style=f"bold {_state_style(run.state)}")
    header.append(f"  since {run.created_at or _MISSING}",
                  style=token("text.muted"))
    parts: list[RenderableType] = [header]
    if run.error:
        parts.append(Text(f"error: {run.error}",
                          style=f"bold {token('state.crit')}"))
        parts.append(Text("(corrupt run: stages and gates are unavailable "
                          "until the log is repaired — nothing is faked)",
                         style=token("text.muted")))
    parts.append(Text("stages", style=f"bold {token('accent')}"))
    parts.append(stages_flow(run))
    parts.append(_stages_table(run))
    parts.append(Text("gates", style=f"bold {token('accent')}"))
    parts.append(gates_table_with_records(run))
    parts.append(Text("cost", style=f"bold {token('accent')}"))
    parts.append(cost_block(run.cost))
    parts.append(Text("events", style=f"bold {token('accent')}"))
    tail = _event_tail(run.events_tail)
    parts.append(tail if tail is not None
                 else Text("(no events)", style=token("text.muted")))
    return Group(*parts)
