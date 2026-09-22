"""GlanceService tests: offline stub legs pin the old dashboard glance
composition (tui/screens/dashboard.py:155-234) — row shapes, chg math,
provenance assembly, history fold, and leg degradation."""

from __future__ import annotations

from interface.services.glance import (
    GlanceCci,
    GlanceService,
    update_history,
)

QT = "Tencent qt.gtimg.cn"
YH = "Yahoo Finance (chart API)"


def _quotes(symbols):
    rows = {
        "000001.SS": {"last": 3300.5, "prev_close": 3280.0, "source": QT},
        "HSI": {"last": 24103.7, "prev_close": 24178.9, "source": QT},
        "QQQ": {"last": 512.06, "prev_close": 507.3, "source": YH},
    }
    return {s: rows[s] for s in symbols if s in rows}


def _cci():
    return {"last": 2345.6, "chg_pct": -0.4}


def _stance(market):
    if market == "hk":
        return {"stance": "cautious", "score": -0.35,
                "source": "fin-daily tape"}
    return None


def _service(**kw) -> GlanceService:
    legs = {"fetch_quotes": _quotes, "fetch_cci": _cci,
            "fetch_stance": _stance}
    legs.update(kw)
    return GlanceService(**legs)


def test_frame_composes_every_leg() -> None:
    frame = _service().frame()
    assert [row.label for row in frame.indices] == [
        "SH COMP", "HSI", "QQX(US)"]  # GLANCE_LABELS order, old 187
    sh = frame.indices[0]
    assert sh.symbol == "000001.SS"
    assert sh.chg_pct == (3300.5 / 3280.0 - 1) * 100.0  # old 196-197
    assert frame.cci == GlanceCci(last=2345.6, chg_pct=-0.4)
    assert [s.market for s in frame.stances] == ["hk"]  # us leg served None
    assert frame.stances[0].stance == "cautious"
    assert not frame.is_empty


def test_provenance_names_serving_sources_only() -> None:
    frame = _service().frame()
    assert frame.sources == ("Tencent", "Yahoo", "ccidx", "fin-daily")


def test_provenance_hides_absent_legs() -> None:
    # Old dashboard.py:227 appended ccidx/fin-daily unconditionally; this
    # copy keeps the line honest to its own comment.
    frame = GlanceService(fetch_quotes=_quotes).frame()
    assert frame.sources == ("Tencent", "Yahoo")
    frame_cci = GlanceService(fetch_quotes=_quotes, fetch_cci=_cci).frame()
    assert frame_cci.sources == ("Tencent", "Yahoo", "ccidx")


def test_missing_quote_symbols_and_prev_close_degrade() -> None:
    def partial(symbols):
        return {"HSI": {"last": 100.0, "source": QT}}  # no prev_close
    frame = GlanceService(fetch_quotes=partial).frame()
    assert [row.symbol for row in frame.indices] == ["HSI"]
    assert frame.indices[0].chg_pct == 0.0  # old 197: no prev_close → 0.0


def test_leg_failures_never_raise() -> None:
    def boom(symbols):
        raise RuntimeError("feed down")
    def boom_cci():
        raise RuntimeError("ws down")
    def boom_stance(market):
        raise RuntimeError("tape down")
    frame = GlanceService(fetch_quotes=boom, fetch_cci=boom_cci,
                          fetch_stance=boom_stance).frame()
    assert frame.indices == () and frame.cci is None
    assert frame.stances == () and frame.sources == ()
    assert frame.is_empty


def test_cci_bad_shapes_degrade() -> None:
    assert GlanceService(fetch_cci=lambda: None).frame().cci is None
    assert GlanceService(fetch_cci=lambda: {"chg_pct": 1.0}).frame().cci \
        is None  # last is None → row absent (old 214)
    bad_chg = GlanceService(
        fetch_cci=lambda: {"last": 10.0, "chg_pct": None}).frame().cci
    assert bad_chg == GlanceCci(last=10.0, chg_pct=0.0)


def test_stance_score_none_renders_zero() -> None:
    frame = GlanceService(
        fetch_stance=lambda m: {"stance": "flat", "score": None}).frame()
    assert frame.stances[0].score == 0.0  # old f-string would have raised


def test_default_service_is_offline_and_empty() -> None:
    frame = GlanceService().frame()
    assert frame.is_empty  # M5 wires the real legs; nothing implicit here


def test_update_history_caps_and_picks_trend() -> None:
    svc = _service()
    history: dict[str, list[float]] = {}
    first = svc.frame()
    history, trend1 = update_history(history, first)
    assert trend1 == []  # first frame: no series has 2 points (old 204-207)
    history, trend2 = update_history(history, svc.frame())
    assert trend2[0] == 3300.5 and len(trend2) == 2
    # Cap: 60 synthetic frames keep only the newest 48 points.
    for _ in range(60):
        history, _t = update_history(history, first)
    assert len(history["000001.SS"]) == 48
    assert history["000001.SS"][-1] == 3300.5
    # Fresh containers: the input mapping is never mutated.
    frozen_in = {"HSI": [1.0]}
    out, _t = update_history(frozen_in, first)
    assert frozen_in == {"HSI": [1.0]} and out is not frozen_in
