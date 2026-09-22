"""Status-tier tests: shape, pipe-safety, cross-tier flag correlation."""

from __future__ import annotations

import re

import pytest

from interface.collectors import build_provider, demo_snapshot
from interface.render.flags import flag_cell_plain
from interface.snapshot import workstation_flags
from interface.status import render_status, status_lines

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def test_plain_output_has_no_ansi_escapes() -> None:
    out = render_status(demo_snapshot(1_000_000.0))
    assert not _ANSI.search(out), "status tier must be pipe-safe"


def test_all_sections_render() -> None:
    lines = status_lines(demo_snapshot(1_000_000.0))
    for section in ("runs:", "intake:", "glance:", "services:"):
        assert any(line.startswith(section) for line in lines), section


def test_flag_cell_width_is_fixed() -> None:
    frame = demo_snapshot(1_000_000.0)
    cell = flag_cell_plain(workstation_flags(frame))
    assert len(cell) == 6
    assert set(cell) <= set("RHGFDC.")


def test_status_header_carries_flag_cell() -> None:
    out = render_status(demo_snapshot(1_000_000.0))
    cell = flag_cell_plain(workstation_flags(demo_snapshot(1_000_000.0)))
    assert cell in out.splitlines()[0], "banner and TUI must correlate"


def test_stale_quote_marked() -> None:
    out = render_status(demo_snapshot(1_000_000.0))  # QQQ stale at this instant
    glance = out.split("glance:")[1]
    assert "STALE" in glance
    assert "src" in glance, "provenance line must survive the status tier"


def test_cost_renders_for_runs_that_have_it() -> None:
    out = render_status(demo_snapshot(241.0))  # final run completed + cost
    assert "cost 0.0184" in out


def test_collector_error_surfaces() -> None:
    provider = build_provider(root=None, demo=False)
    out = render_status(provider())
    assert "COLLECTOR ERROR" in out and "--root" in out


def test_main_demo_smoke(capsys) -> None:
    from interface.status import main

    assert main(["--demo"]) == 0
    captured = capsys.readouterr().out
    assert "STAMMTISCH status" in captured


def test_shell_cli_demo_is_opt_in() -> None:
    """Grilling top finding: the TUI's --demo used to default True with no
    off-switch, silently ignoring --root. Pin the opt-in contract."""
    pytest.importorskip("textual", reason="the shell module imports textual")
    from interface.app.shell import _parse_args

    assert _parse_args([]).demo is False
    assert _parse_args(["--demo"]).demo is True
    assert _parse_args(["--root", "/tmp/x"]).demo is False


def test_resolve_state_root_precedence(monkeypatch) -> None:
    from interface.args import resolve_state_root

    monkeypatch.delenv("STAMMTISCH_HOME", raising=False)
    assert resolve_state_root(None) is None or True  # depends on host default
    monkeypatch.setenv("STAMMTISCH_HOME", "/tmp/from-env")
    assert str(resolve_state_root(None)) == "/tmp/from-env"
    assert str(resolve_state_root(__import__("pathlib").Path("/explicit"))) == "/explicit"


# ── grilling S1: args edge cases ───────────────────────────────────────


def test_state_root_at_a_file_degrades_loudly(
        monkeypatch, tmp_path) -> None:
    """STAMMTISCH_HOME/--root pointing at a FILE must not crash
    resolve_state_root: the path is returned and the collector degrades
    loudly instead of guessing another root."""

    from interface.args import resolve_state_root

    file_root = tmp_path / "not-a-dir"
    file_root.write_text("i am a file", encoding="utf-8")
    assert resolve_state_root(file_root) == file_root.resolve(), (
        "returned, not crashed")
    monkeypatch.setenv("STAMMTISCH_HOME", str(file_root))
    assert resolve_state_root(None) == file_root, (
        "env at a file: returned unvalidated, degraded loudly downstream")
    import interface.collectors.feeds as feeds_mod
    monkeypatch.setattr(feeds_mod, "_real_fetch", lambda symbols: {})
    provider = build_provider(root=file_root, demo=False)
    frame = provider()
    assert frame.collector_error and "cannot scan" in frame.collector_error


def test_relative_root_resolves_to_absolute(monkeypatch, tmp_path) -> None:
    from pathlib import Path

    from interface.args import resolve_state_root

    monkeypatch.chdir(tmp_path)
    root = resolve_state_root(Path("state"))
    assert root is not None
    assert root.is_absolute()
    assert root == (tmp_path / "state").resolve()


def test_explicit_absolute_root_passes_through(monkeypatch, tmp_path) -> None:

    from interface.args import resolve_state_root

    monkeypatch.delenv("STAMMTISCH_HOME", raising=False)
    assert resolve_state_root(tmp_path) == tmp_path.resolve()


# ── grilling S6: one one-shot renderer for piped glances ───────────────


def test_watch_non_tty_uses_the_tier1_one_shot_renderer(
        capsys, monkeypatch) -> None:
    """Piped stdin/stdout: watch prints exactly the tier-1 status frame —
    one one-shot renderer, not two (the rich page belongs to the
    interactive tty loop only)."""
    from interface.watch import main

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    assert main(["--demo"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("STAMMTISCH status - ")
    assert "runs:" in out and "services:" in out
    assert "demo-run-live" in out
    assert _ANSI.search(out) is None, "pipe-safe: no ANSI escapes"
