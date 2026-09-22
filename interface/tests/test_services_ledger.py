"""LedgerService tests: the read contract and FIFO fold copied from
tui/portfolio.py:26-138, plus the injectable mark pass — long/short
lot math, realized P&L, weighted average cost, corrupt-file honesty,
and the honest no-mark path."""

from __future__ import annotations

import json

import pytest

from interface.services.ledger import (
    LedgerError,
    LedgerService,
    ledger_path,
    load_fills,
)


def _write_ledger(root, fills, name="ledger.json", text=None):
    path = root / "intel" / "portfolio" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None
                    else json.dumps({"version": 1, "fills": fills}),
                    encoding="utf-8")
    return path


def test_ledger_path_is_the_old_contract(tmp_path) -> None:
    assert ledger_path(tmp_path) == (
        tmp_path / "intel" / "portfolio" / "ledger.json")


def test_load_missing_file_is_empty_and_none_root_is_empty(tmp_path) -> None:
    assert load_fills(tmp_path) == []
    assert load_fills(None) == []


def test_load_rejects_corrupt_files(tmp_path) -> None:
    _write_ledger(tmp_path, [], text="not json{")
    with pytest.raises(LedgerError):
        load_fills(tmp_path)
    _write_ledger(tmp_path, [], text=json.dumps({"no_fills": 1}))
    with pytest.raises(LedgerError):
        load_fills(tmp_path)


def _fill(ts, side, symbol, qty, price, broker="ib", fill_id="f1",
          fee=0.0):
    return {"id": fill_id, "ts": ts, "broker": broker, "symbol": symbol,
            "side": side, "qty": qty, "price": price, "fee": fee}


def test_fifo_fold_buy_then_partial_sell(tmp_path) -> None:
    """Two buys, one partial sell: oldest lot closes first, realized
    P&L books at the sell price, avg cost re-weights (portfolio.py:94+)."""
    fills = [
        _fill("2026-01-01T09:00:00", "buy", "AAPL", 100, 100.0),
        _fill("2026-01-02T09:00:00", "buy", "AAPL", 100, 120.0),
        _fill("2026-01-03T09:00:00", "sell", "AAPL", 60, 150.0),
    ]
    _write_ledger(tmp_path, fills)
    frame = LedgerService(quotes=lambda symbols: {}).snapshot(tmp_path)
    assert frame.ok
    (row,) = frame.positions
    # Closed 60 of the FIRST lot @150 vs 100: realized = 60 * 50 = 3000.
    assert row.realized_pnl == pytest.approx(3000.0)
    # Remaining: 40 @100 + 100 @120 -> avg = (4000 + 12000) / 140.
    assert row.net_qty == pytest.approx(140.0)
    assert row.avg_cost == pytest.approx((40 * 100 + 100 * 120) / 140)
    assert row.last is None and row.unrealized is None, (
        "no quotes injected: the mark stays honestly absent")


def test_fifo_fold_short_leg(tmp_path) -> None:
    """An excess sell opens a short lot a later buy closes (portfolio.py
    short branch: realized = lot price - close price)."""
    fills = [
        _fill("2026-01-01T09:00:00", "sell", "XYZ", 50, 200.0),
        _fill("2026-01-02T09:00:00", "buy", "XYZ", 20, 180.0),
    ]
    _write_ledger(tmp_path, fills)
    frame = LedgerService(quotes=lambda symbols: {}).snapshot(tmp_path)
    (row,) = frame.positions
    assert row.realized_pnl == pytest.approx(20 * (200.0 - 180.0))
    assert row.net_qty == pytest.approx(-30.0)
    assert row.avg_cost == pytest.approx(200.0)


def test_fold_is_per_broker_symbol_and_sorted(tmp_path) -> None:
    fills = [
        _fill("2026-01-01T09:00:00", "buy", "B", 1, 10.0, broker="zeta"),
        _fill("2026-01-01T09:00:00", "buy", "A", 1, 10.0, broker="alpha"),
        _fill("2026-01-01T09:00:00", "buy", "A", 1, 10.0, broker="zeta"),
    ]
    _write_ledger(tmp_path, fills)
    frame = LedgerService(quotes=lambda symbols: {}).snapshot(tmp_path)
    assert [(r.broker, r.symbol) for r in frame.positions] == [
        ("alpha", "A"), ("zeta", "A"), ("zeta", "B")]


def test_fold_skips_unparseable_fills_without_losing_the_rest(tmp_path) -> None:
    fills = [
        {"id": "bad", "ts": "x", "side": "buy", "symbol": "?", "qty": "n/a",
         "price": "n/a"},
        _fill("2026-01-01T09:00:00", "buy", "AAPL", 10, 5.0, fill_id="ok"),
    ]
    _write_ledger(tmp_path, fills)
    frame = LedgerService(quotes=lambda symbols: {}).snapshot(tmp_path)
    assert len(frame.positions) == 1
    assert frame.positions[0].net_qty == pytest.approx(10.0)
    assert [f.id for f in frame.fills] == ["bad", "ok"], (
        "the fills table lists the raw file order, honest about bad rows")


def test_mark_pass_uses_the_injected_quote_fn(tmp_path) -> None:
    _write_ledger(tmp_path, [
        _fill("2026-01-01T09:00:00", "buy", "AAPL", 10, 100.0)])
    quotes = lambda symbols: {"AAPL": {"last": 130.0, "source": "test"}}
    frame = LedgerService(quotes=quotes).snapshot(tmp_path)
    (row,) = frame.positions
    assert row.last == pytest.approx(130.0)
    assert row.unrealized == pytest.approx((130.0 - 100.0) * 10)


def test_mark_pass_degrades_when_the_quote_fn_raises(tmp_path) -> None:
    _write_ledger(tmp_path, [
        _fill("2026-01-01T09:00:00", "buy", "AAPL", 10, 100.0)])

    def boom(symbols):
        raise RuntimeError("feed down")

    frame = LedgerService(quotes=boom).snapshot(tmp_path)
    assert frame.ok, "a dead quote leg degrades to no marks, never an error"
    assert frame.positions[0].last is None


def test_snapshot_error_frame_on_corrupt_ledger(tmp_path) -> None:
    _write_ledger(tmp_path, [], text="{broken")
    frame = LedgerService().snapshot(tmp_path)
    assert not frame.ok
    assert frame.error and "unreadable" in frame.error


def test_load_seam_is_injectable(tmp_path) -> None:
    service = LedgerService(load=lambda root: [
        _fill("2026-01-01T09:00:00", "buy", "AAPL", 1, 1.0)])
    frame = service.snapshot("ignored-root")
    assert frame.ok and frame.positions[0].net_qty == pytest.approx(1.0)
