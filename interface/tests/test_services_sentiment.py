"""SentimentService tests: the report load contract copied from
tui/brief.py:144-220, the tape scorer/formatter copied from
tui/tape.py, and the adapted history index — all against tmp fixtures
(no real workspace path is ever read)."""

from __future__ import annotations

import json

import pytest

from interface.services.sentiment import (
    build_tape,
    classify,
    desk_sentiment,
    format_tape,
    history_entries,
    list_dates,
    load_daily,
    load_daily_path,
    resolve_reports_root,
    tone,
)


def _legacy_root(tmp_path, days):
    """Build a legacy ``YYYYMMDD/output`` tree, refined preferred."""
    for day, brief in days:
        out = tmp_path / day / "output"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"fin-daily-{day}.refined.json").write_text(
            json.dumps({"date": day, "model": "test-model",
                        "brief": brief, "markets": {}, "notes": []}),
            encoding="utf-8")
    return tmp_path


# ── the tape scorer (tui/tape.py) ──────────────────────────────────────


def test_classify_and_tone_vocabulary() -> None:
    assert classify("央行 发布 货币政策") == "hard"
    assert classify("Fed cuts rates by 25bps") == "hard"
    assert classify("sources say the deal is unconfirmed") == "rumor"
    assert classify("this stock soars to the moon, meme fomo") == "hype"
    assert classify("southbound etf flow surges") == "flow"
    assert classify("Nasdaq and S&P close higher") == "tape"
    assert classify("a quiet day at the office") == "soft"
    assert tone("beat and upgrade") == pytest.approx(1.0)
    assert tone("miss and downgrade") == pytest.approx(-1.0)
    assert tone("nothing to see") == 0.0


def test_build_tape_stance_bands_and_hype_cap() -> None:
    bearish = ["revenue miss as profits drop"] * 6
    tape = build_tape(bearish, {}, [])
    assert tape["stance"] == "defensive" and tape["n"] == 6
    assert tape["score"] < -0.28

    bullish = ["earnings beat drives gains"] * 6
    assert build_tape(bullish, {}, [])["stance"] == "constructive"

    mixed = (["revenue miss as profits drop"] * 3
             + ["earnings beat drives gains"] * 3)
    assert build_tape(mixed, {}, [])["stance"] == "watch", (
        "a genuinely split tape with |score| < 0.12 is watch")

    hyped = ["this soars, pure fomo mooning parabolic"] * 4
    hype_tape = build_tape(hyped, {}, [])
    assert hype_tape["stance"] == "watch", (
        "hype share >= 0.35 forces watch even when the score is bull")
    assert any("do not chase" in c for c in hype_tape["caveats"])


def test_build_tape_from_records_scopes_markets() -> None:
    records = [
        {"market": "us", "title": "Fed", "summary": "raises rates",
         "source_labels": ["reuters"]},
        {"market": "crypto", "title": "BTC", "summary": "mooning"},
        {"market": "hk", "title": "恒指", "summary": "放量 上涨"},
    ]
    tape = build_tape(None, None, records=records)
    assert tape["n"] == 2, "out-of-scope captures never reach the tape"
    assert set(tape["by_market"]) == {"ashare", "hk", "us"}


def test_format_tape_english_chrome_verbatim() -> None:
    tape = build_tape([], {"hk": [{"title": "恒指 放量 上涨"}] * 2},
                      ["note one"])
    text = format_tape(tape)
    assert "--- MARKET SENTIMENT ---" in text
    assert "STANCE=" in text and "SCORE=" in text and "HYPE=" in text
    assert "HK +0.55 (HYPE 0/2)" in text, (
        "tape-weighted score renders in the market bit")
    assert "! note one" in text
    assert format_tape(None) == ""


def test_desk_sentiment_empty_doc_is_blank() -> None:
    assert desk_sentiment({}) == ""
    assert desk_sentiment({"tape": {"stance": "watch", "score": 0.0}})


# ── the report load contract (tui/brief.py) ────────────────────────────


def test_load_daily_latest_and_one_dated(tmp_path) -> None:
    _legacy_root(tmp_path, [("20260101", []), ("20260102", [])])
    latest = load_daily(reports_root=tmp_path)
    assert latest["ok"] and latest["date"] == "20260102"
    assert latest["model"] == "test-model"
    assert latest["tape"]["n"] == 0, "no scoreable prose -> empty tape"
    dated = load_daily("20260101", reports_root=tmp_path)
    assert dated["ok"] and dated["date"] == "20260101"
    assert load_daily("not-a-date", reports_root=tmp_path)["ok"] is False
    empty = tmp_path / "empty"
    empty.mkdir()
    result = load_daily(reports_root=empty)
    assert result["ok"] is False and "no daily JSON" in result["error"]


def test_load_daily_path_validates_the_date_and_strips_t(tmp_path) -> None:
    path = tmp_path / "fin-daily-20260102.refined.json"
    path.write_text(json.dumps({"date": "2026-01-02"}), encoding="utf-8")
    doc = load_daily_path(path, expected_date="20260102")
    assert doc["ok"] and doc["date"] == "20260102"
    mismatch = load_daily_path(path, expected_date="20260101")
    assert mismatch["ok"] is False and "does not match" in mismatch["error"]
    bad = tmp_path / "bad.json"
    bad.write_text("{nope", encoding="utf-8")
    assert load_daily_path(bad)["ok"] is False
    (tmp_path / "list.json").write_text("[]", encoding="utf-8")
    assert load_daily_path(tmp_path / "list.json")["error"] == (
        "JSON root is not an object")


def test_load_daily_path_scores_the_canonical_sibling(tmp_path) -> None:
    """A report with no embedded tape scores the sibling canonical
    dataset (the uncurated newswire); a date mismatch fails closed."""
    day = "20260103"
    jpath = tmp_path / f"fin-daily-{day}.json"
    jpath.write_text(json.dumps({
        "date": day, "brief": [], "markets": {}, "notes": [],
    }), encoding="utf-8")
    (tmp_path / "canonical-dataset.json").write_text(json.dumps({
        "date": day,
        "records": [
            {"market": "us", "title": "Fed cuts rates",
             "summary": "wholesale prices beat expectations"},
            {"market": "us", "title": "Tech", "summary": "misses revenue"},
        ],
    }), encoding="utf-8")
    doc = load_daily_path(jpath)
    assert doc["ok"] and doc["tape"]["n"] == 2

    (tmp_path / "canonical-dataset.json").write_text(json.dumps({
        "date": "19990101", "records": [{"market": "us", "title": "x",
                                         "summary": "y"}]}),
        encoding="utf-8")
    stale = load_daily_path(jpath)
    assert stale["ok"] and stale["tape"]["n"] == 0, (
        "a mismatched sibling is ignored, never trusted")


def test_list_dates_ignores_dirs_without_a_product(tmp_path) -> None:
    _legacy_root(tmp_path, [("20260101", [])])
    (tmp_path / "20260105").mkdir()
    (tmp_path / "notadate").mkdir()
    assert list_dates(tmp_path) == ["20260101"]


def test_resolve_reports_root_order(monkeypatch, tmp_path) -> None:
    assert resolve_reports_root(tmp_path) == tmp_path
    monkeypatch.delenv("GALAHAD_REPORTS_ROOT", raising=False)
    monkeypatch.setenv("STAMMTISCH_REPORTS", str(tmp_path / "env"))
    assert resolve_reports_root(None) == tmp_path / "env"
    monkeypatch.delenv("STAMMTISCH_REPORTS", raising=False)
    monkeypatch.setenv("GALAHAD_REPORTS_ROOT", str(tmp_path / "gal"))
    assert resolve_reports_root(None) == tmp_path / "gal"


# ── the history index (adapted from tui/history.py) ────────────────────


def test_history_entries_scans_both_shapes_newest_first(tmp_path) -> None:
    _legacy_root(tmp_path, [("20260101", []), ("20260103", [])])
    workspace = tmp_path / "ws"
    run_dir = workspace / "runs" / "run-42"
    run_dir.mkdir(parents=True)
    (run_dir / "fin-daily-20260103.json").write_text(
        json.dumps({"date": "20260103"}), encoding="utf-8")
    (run_dir / "fin-daily-20260103.html").write_text("<html>",
                                                     encoding="utf-8")
    entries = history_entries(reports_root=tmp_path, workspace_root=workspace)
    assert [e.report_date for e in entries] == ["20260103", "20260101"]
    top = entries[0]
    assert top.origin == "intake", "intake outranks legacy on the same day"
    assert top.json_path.endswith("fin-daily-20260103.json")
    assert top.html_path.endswith(".html")


def test_history_entries_fail_closed_per_file(tmp_path) -> None:
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    # Name/date mismatch: the artifact name is the anchor -> skipped.
    (run_dir / "fin-daily-20260104.json").write_text(
        json.dumps({"date": "20251231"}), encoding="utf-8")
    # No in-doc date: intake requires one -> skipped.
    (run_dir / "fin-daily-20260105.json").write_text("{}", encoding="utf-8")
    # Unreadable JSON -> skipped.
    (run_dir / "fin-daily-20260106.json").write_text("{oops",
                                                     encoding="utf-8")
    assert history_entries(reports_root=tmp_path) == ()


def test_history_entries_one_root_can_host_both_shapes(tmp_path) -> None:
    _legacy_root(tmp_path, [("20260101", [])])
    run_dir = tmp_path / "runs" / "r"
    run_dir.mkdir(parents=True)
    (run_dir / "fin-daily-20260102.json").write_text(
        json.dumps({"date": "20260102"}), encoding="utf-8")
    entries = history_entries(reports_root=tmp_path)
    assert [e.report_date for e in entries] == ["20260102", "20260101"]
