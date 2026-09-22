"""DecisionReader tests: decisions/latest.json parsing and the SECURITY
watchlist merge (tui/screens/domains.py:1144-1230, 1661-1691) with
synthetic decision files — defensive shapes pinned clause-for-clause."""

from __future__ import annotations

import json
from pathlib import Path

from interface.services.decisions import (
    decision_positions,
    decision_symbols,
    security_watchlist,
)

V1 = {"decision_version": 1}


def _write_decisions(root: Path, payload: dict | None) -> Path:
    folder = root / "decisions"
    folder.mkdir(parents=True, exist_ok=True)
    if payload is not None:
        (folder / "latest.json").write_text(
            json.dumps(payload), encoding="utf-8")
    return root


def _full_payload() -> dict:
    return {
        "decision_version": 1,
        "zones": {
            "A-SHARE": {
                "positions": [
                    {"symbol": "600519.SS", "action": "buy",
                     "thesis": "core liquor hold"},
                    {"symbol": "000001.SS", "action": "cut",
                     "thesis": "index hedge off"},
                ],
                "scan_top": [
                    {"symbol": "002415.SZ", "tr": 12.3, "sharpe": 1.4,
                     "maxdd": 8, "win": 55, "trades": 42},
                    {"symbol": "600519.SS", "tr": 1.0},  # loses to position
                ],
            },
            "HK": {
                "positions": [
                    {"symbol": "0700.HK", "action": "hold", "reason": "via r"},
                ],
                "scan_top": [],
            },
        },
    }


# ── decision_positions (old domains.py:1661-1691) ─────────────────────


def test_positions_merge_positions_and_scan_ranks(tmp_path: Path) -> None:
    root = _write_decisions(tmp_path, _full_payload())
    out = decision_positions(root)
    assert out["600519.SS"].action == "buy"  # position wins over scan_top
    assert out["600519.SS"].thesis == "core liquor hold"
    assert out["002415.SZ"].action == "#1"
    assert out["002415.SZ"].thesis == (
        "scan #1 TR12.3% Sharpe1.4 DD-8% win55% 42笔")  # old card format
    assert out["0700.HK"].thesis == "via r"  # reason fallback (old 1673-74)
    assert out["000001.SS"].action == "cut"


def test_positions_thesis_truncated_to_120(tmp_path: Path) -> None:
    root = _write_decisions(tmp_path, {"decision_version": 1, "zones": {
        "US": {"positions": [
            {"symbol": "QQQ", "action": "hold", "thesis": "x" * 300}]}}})
    assert len(decision_positions(root)["QQQ"].thesis) == 120


def test_missing_file_is_empty(tmp_path: Path) -> None:
    assert decision_positions(tmp_path) == {}
    assert decision_symbols(tmp_path) == []


def test_unsupported_version_is_empty(tmp_path: Path) -> None:
    root = _write_decisions(
        tmp_path, {"decision_version": 2, "zones": {"US": {"positions": [
            {"symbol": "QQQ", "action": "buy"}]}}})
    assert decision_positions(root) == {}
    assert decision_symbols(root) == []


def test_malformed_shapes_degrade_to_empty(tmp_path: Path) -> None:
    folder = tmp_path / "decisions"
    folder.mkdir(parents=True)
    (folder / "latest.json").write_text("{not json", encoding="utf-8")
    assert decision_positions(tmp_path) == {}
    # zones not a dict → empty; a non-dict zone/position/scan row is
    # skipped, not fatal (old 1661 read lost the whole file here).
    root = _write_decisions(tmp_path, {
        "decision_version": 1, "zones": {"A-SHARE": "bad", "HK": {
            "positions": ["nope", {"symbol": "0700.HK", "action": "hold"}],
            "scan_top": [7, {"symbol": "0005.HK"}]}}})
    out = decision_positions(root)
    assert set(out) == {"0700.HK", "0005.HK"}


# ── decision_symbols (old domains.py:1144-1184) ───────────────────────


def test_symbols_exclude_cut_and_include_scan_top(tmp_path: Path) -> None:
    root = _write_decisions(tmp_path, _full_payload())
    assert decision_symbols(root) == [
        "600519.SS", "002415.SZ", "600519.SS", "0700.HK"]
    # scan_top dupes are kept here (dedupe happens in the watchlist merge)


# ── security_watchlist (old domains.py:1195-1230) ─────────────────────


def test_manual_anchors_then_decisions_then_recents(tmp_path: Path) -> None:
    root = _write_decisions(tmp_path, _full_payload())
    out = security_watchlist(
        manual=["0700.HK", "600519"],  # 600519 normalizes to 600519.SS
        recents=["BRK.B", "0700.hk", "  "],
        state_root=root,
    )
    # 000001.SS never appears: cut positions do not flow to the board.
    # BRK.B keeps its dot — the engine resolver only rewrites numeric
    # CN/HK codes and known exchange suffixes (not US class shares).
    assert out == ["0700.HK", "600519.SS", "002415.SZ",
                   "BRK.B"]  # first-seen order; dupes and blanks dropped


def test_empty_manual_still_flows_decisions(tmp_path: Path) -> None:
    """Empty manual list is a full-market-screen signal, not an empty
    board (old domains.py:1197-1201)."""
    root = _write_decisions(tmp_path, _full_payload())
    assert security_watchlist([], recents=None, state_root=root) == [
        "600519.SS", "002415.SZ", "0700.HK"]


def test_no_manual_no_decisions_leaves_recents(tmp_path: Path) -> None:
    assert security_watchlist([], recents=["HSI"], state_root=tmp_path) \
        == ["HSI"]
    assert security_watchlist(None, None, tmp_path) == []
