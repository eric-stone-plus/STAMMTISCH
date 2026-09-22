"""Feeds lane tests (M4): the services livefeed-contract copy and the
collector-side provider. The real network path is NEVER exercised here —
every fetch is a stub; the pin proves QuoteSnapshot rows with per-symbol
ages, TTL batching, and the keep-last-good STALE behavior."""

from __future__ import annotations

from datetime import datetime

import pytest

import interface.collectors.feeds as feeds_mod
from interface.collectors.feeds import FeedsCollector
from interface.collectors.session import CollectorSession
from interface.services.feeds import (
    CN_TZ,
    parse_batch,
    quote_age_s,
    to_tencent_code,
)
from interface.snapshot import QUOTE_STALE_S, WorkstationSnapshot, workstation_flags

QT = "Tencent qt.gtimg.cn"
YH = "Yahoo Finance (chart API)"

#: A fixed CN-side stamp: 2026-09-22 15:00:00 Asia/Shanghai.
CN_STAMP = datetime(2026, 9, 22, 15, 0, 0, tzinfo=CN_TZ)


def _tencent_row(last: float = 3300.5, prev: float = 3280.0,
                 time: str = "20260922150000") -> dict:
    return {"last": last, "prev_close": prev, "source": QT, "time": time,
            "name": "SH COMP"}


def _yahoo_row(last: float = 512.06, prev: float = 507.3,
               time: str = "") -> dict:
    return {"last": last, "prev_close": prev, "source": YH, "time": time}


# ── THE PIN: stub fetch → QuoteSnapshot rows with ages ────────────────


def test_stub_fetch_produces_quote_snapshots_with_ages() -> None:
    now = CN_STAMP.timestamp() + 120.0  # CN row is 120s old
    yahoo_time = str(int(now - 45.0))   # epoch-seconds string
    calls: list[list[str]] = []

    def stub(symbols):
        calls.append(list(symbols))
        return {"000001.SS": _tencent_row(),
                "QQQ": _yahoo_row(time=yahoo_time)}

    collector = FeedsCollector(["000001.SS", "QQQ"],
                               labels={"000001.SS": "SH COMP",
                                       "QQQ": "QQX(US)"},
                               fetch=stub)
    rows = collector.collect(now=now)
    assert calls == [["000001.SS", "QQQ"]]
    by_symbol = {row.symbol: row for row in rows}
    sh = by_symbol["000001.SS"]
    assert (sh.label, sh.last, sh.source) == ("SH COMP", 3300.5, QT)
    assert sh.change_pct == (3300.5 / 3280.0 - 1.0) * 100.0
    assert sh.age_s == pytest.approx(120.0)  # CN wall clock normalized
    qqq = by_symbol["QQQ"]
    assert (qqq.label, qqq.source) == ("QQX(US)", YH)
    assert qqq.age_s == pytest.approx(45.0)  # epoch seconds normalized


def test_ttl_batches_refreshes_not_per_tick() -> None:
    now = CN_STAMP.timestamp()
    collector = FeedsCollector(["HSI"], fetch=lambda s: {
        "HSI": _tencent_row(last=1.0, time="20260922150000")})
    collector.collect(now=now)
    collector.collect(now=now + 10.0)  # inside the TTL window
    assert collector.fetch_calls == 1
    collector.collect(now=now + 31.0)  # past 30s → one refresh
    assert collector.fetch_calls == 2
    assert collector.collect(now=now + 31.0)[0].last == 1.0


def test_failed_refresh_keeps_last_good_and_ages_it() -> None:
    now = CN_STAMP.timestamp()
    state = {"fail": False}

    def flaky(symbols):
        if state["fail"]:
            raise RuntimeError("feed down")
        return {"HSI": _tencent_row()}

    collector = FeedsCollector(["HSI"], fetch=flaky)
    assert collector.collect(now=now)  # good fetch
    state["fail"] = True
    rows = collector.collect(now=now + 200.0)  # refresh fails, row ages
    assert len(rows) == 1
    assert rows[0].age_s == pytest.approx(200.0)
    assert rows[0].age_s > QUOTE_STALE_S  # STALE territory, row kept
    frame = WorkstationSnapshot(taken_at=now + 200.0, quotes=rows)
    assert workstation_flags(frame)["F"]  # the contract lights the flag


def test_first_failure_renders_no_quotes() -> None:
    collector = FeedsCollector(["HSI"], fetch=lambda s: (_ for _ in ()).throw(
        RuntimeError("down")))
    assert collector.collect(now=CN_STAMP.timestamp()) == ()


def test_no_prev_close_is_zero_change_and_undatable_ages_from_fetch() -> None:
    now = CN_STAMP.timestamp()
    collector = FeedsCollector(["HSI"], ttl_s=60.0, fetch=lambda s: {
        "HSI": {"last": 100.0, "prev_close": None, "source": QT,
                "time": ""}})
    collector.collect(now=now)
    row = collector.collect(now=now + 30.0)[0]  # same window, no refetch
    assert row.change_pct == 0.0
    assert row.age_s == pytest.approx(30.0)  # undatable → age from fetch


# ── services.feeds contract copy (offline units) ──────────────────────


def test_to_tencent_code_mapping() -> None:
    assert to_tencent_code("HSI") == "hkHSI"
    assert to_tencent_code("600519.SS") == "sh600519"
    assert to_tencent_code("000001.SZ") == "sz000001"
    assert to_tencent_code("0700.HK") == "hk00700"
    assert to_tencent_code("QQQ") == "usQQQ"
    assert to_tencent_code("BRK-B.X") is None  # unmappable stays absent


def _batch_body() -> str:
    fields = [""] * 46
    fields[1] = "SH COMP"
    fields[3] = "3300.50"
    fields[4] = "3280.00"
    fields[5] = "3290.00"
    fields[6] = "123000"
    fields[30] = "20260922150000"
    fields[33] = "3310.00"
    fields[34] = "3270.00"
    return f'v_sh0000001="{"~".join(fields)}";'


def test_parse_batch_field_map() -> None:
    rows = parse_batch(_batch_body())
    row = rows["sh0000001"]
    assert row["last"] == 3300.50 and row["prev_close"] == 3280.00
    assert row["high"] == 3310.00 and row["low"] == 3270.00
    assert row["time"] == "20260922150000"
    assert row["source"] == QT
    # Guard clauses: short payloads and priceless rows are dropped.
    assert parse_batch('v_sh0000002="1~2~3"') == {}
    fields = [""] * 46
    assert parse_batch(f'v_sh0000003={"~".join(fields)}') == {}


def test_quote_age_s_normalizes_every_provider_format() -> None:
    now = CN_STAMP.timestamp() + 60.0
    assert quote_age_s({"time": "20260922150000"}, now) == \
        pytest.approx(60.0)  # CN YYYYMMDDHHMMSS
    assert quote_age_s({"time": "2026-09-22 15:00:00"}, now) == \
        pytest.approx(60.0)  # CN dashed form
    assert quote_age_s({"time": "2026/09/22 15:00:00"}, now) == \
        pytest.approx(60.0)  # CN slash form (HK rows on the CN feed)
    assert quote_age_s({"time": str(now - 30.0)}, now) == \
        pytest.approx(30.0)  # Yahoo epoch seconds
    assert quote_age_s({"time": str(now + 30.0)}, now) == 0.0  # future
    assert quote_age_s({"time": ""}, now) is None
    assert quote_age_s({"time": "garbage"}, now) is None


# ── session wiring: the real path assembles quotes; demo never does ──


def test_session_assembles_quotes_with_glance_labels(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feeds_mod, "_real_fetch", lambda symbols: {
        "000001.SS": _tencent_row(),
        "QQQ": _yahoo_row(time=str(CN_STAMP.timestamp()))})
    session = CollectorSession(tmp_path)  # unique root → own session
    frame = session.snapshot()
    labels = {row.symbol: row.label for row in frame.quotes}
    assert labels == {"000001.SS": "SH COMP", "QQQ": "QQX(US)"}


def test_session_feeds_failure_never_raises(tmp_path,
                                            monkeypatch) -> None:
    def down(symbols):
        raise RuntimeError("network gone")

    monkeypatch.setattr(feeds_mod, "_real_fetch", down)
    session = CollectorSession(tmp_path)
    frame = session.snapshot()
    assert frame.quotes == ()
    # A feeds outage is not a collector error: the other planes still
    # assembled (their own errors, if any, never mention the feed).
    assert "network gone" not in (frame.collector_error or "")
