"""Tier 1 — one-shot workstation summary (stdlib-only, pipe-safe).

``python -m interface.status [--demo] [--root PATH]`` renders one frozen
snapshot as a plain table and exits: the cron/SSH glance. Uncolored by
design (grep-friendly); the flag cell is the same fixed-width shape the
TUI renders, so rows correlate across tiers by text.

The content builder (:func:`status_lines`) is shared by every consumer —
rich layout sugar, when it lands, wraps the same lines.
"""

from __future__ import annotations

import argparse
import sys
import time

from interface.args import add_data_args, resolve_state_root
from interface.snapshot import (
    QUOTE_STALE_S,
    WorkstationSnapshot,
    workstation_flags,
)

_STALE_MARK = " STALE"


def _fmt_age(age_s: float) -> str:
    if age_s < 90:
        return f"{age_s:.0f}s"
    if age_s < 5_400:
        return f"{age_s / 60:.0f}m"
    return f"{age_s / 3_600:.1f}h"


def _flag_cell(snapshot: WorkstationSnapshot) -> str:
    from interface.render.flags import flag_cell_plain

    return flag_cell_plain(workstation_flags(snapshot))


def status_lines(snapshot: WorkstationSnapshot) -> list[str]:
    """The whole one-shot summary as plain lines (no ANSI escapes)."""
    from interface.render.flags import flag_cell_plain
    from interface.snapshot import run_flags

    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snapshot.taken_at))
    lines = [f"STAMMTISCH status - {stamp} - {_flag_cell(snapshot)}"]
    if snapshot.collector_error:
        lines.append(f"COLLECTOR ERROR: {snapshot.collector_error}")

    lines.append("")
    lines.append("runs:")
    if not snapshot.runs:
        lines.append("  (none)")
    for run in snapshot.runs:
        flags = flag_cell_plain(run_flags(run))
        gates = ""
        if run.gates:
            passed = sum(1 for g in run.gates if g.decision == "PASS")
            gates = f"  gates {passed}/{len(run.gates)}"
        cost = f"  cost {run.cost.total:.4f}" if run.cost else ""
        lines.append(
            f"  {run.id:<24} {run.state:<10} {flags}{gates}{cost}")
        if run.error:
            lines.append(f"    error: {run.error}")

    lines.append("")
    lines.append("intake:")
    if not snapshot.intake:
        lines.append("  (none)")
    for session in snapshot.intake:
        date = f"  {session.report_date}" if session.report_date else ""
        lines.append(f"  {session.id:<24} {session.state:<12}{date}")

    lines.append("")
    lines.append("glance:")
    if not snapshot.quotes:
        lines.append("  (no quotes)")
    for quote in snapshot.quotes:
        stale = _STALE_MARK if quote.age_s > QUOTE_STALE_S else ""
        lines.append(
            f"  {quote.label:<8} {quote.last:>12,.2f} {quote.change_pct:+6.2f}%"
            f"  {_fmt_age(quote.age_s):>5} old{stale}  src {quote.source}")

    lines.append("")
    lines.append("services:")
    if not snapshot.services:
        lines.append("  (none)")
    for service in snapshot.services:
        state = "ok" if service.available else "DOWN"
        detail = f"  {service.detail}" if service.detail else ""
        lines.append(f"  {service.name:<10} {state}{detail}")
    return lines


def render_status(snapshot: WorkstationSnapshot) -> str:
    """One-shot summary as a single string (trailing newline included)."""
    return "\n".join(status_lines(snapshot)) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="interface.status",
        description="One-shot STAMMTISCH workstation summary.")
    add_data_args(parser)
    args = parser.parse_args(argv)
    from interface.collectors import build_provider

    provider = build_provider(resolve_state_root(args.root), demo=args.demo)
    sys.stdout.write(render_status(provider()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
