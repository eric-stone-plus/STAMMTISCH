"""Board row-merge protocol tests — the futures 3-leg merge contract
(tui/screens/domains.py:537-589): late legs never wipe standing rows,
pending placeholders survive, the quote pass updates in place."""

from __future__ import annotations

from interface.services.board_merge import (
    BoardRow,
    apply_quote,
    is_merged,
    is_pending,
    merge_leg,
    placeholder,
)


def _merged_yahoo(key: str = "HG25", last: float = 5_234.0) -> BoardRow:
    return BoardRow(key=key, source="yahoo", last=last, chg_pct=0.8,
                    pct5=1.2, pct20=-0.4, volume=123.0, unit="CNY",
                    curve=({"month": "DEC26", "settle": 5200.0},))


# ── quote pass (old domains.py:558-589) ───────────────────────────────


def test_quote_pass_overlays_fields_and_clears_error() -> None:
    row = apply_quote(placeholder("HG25"), {
        "last": 5300.0, "chg_pct": 1.3, "volume": 456.0,
        "recent": [{"date": "2026-09-22", "close": 5300.0}],
    })
    assert (row.last, row.chg_pct, row.volume) == (5300.0, 1.3, 456.0)
    assert row.error is None  # old 580: item.pop("error", None)
    assert row.unit == "" and row.curve == ()  # scaffold defaults restored
    assert row.recent == ({"date": "2026-09-22", "close": 5300.0},)


def test_quote_pass_keeps_standing_scaffold() -> None:
    row = apply_quote(_merged_yahoo(), {"last": 5_250.0})
    assert row.unit == "CNY" and row.curve  # setdefault semantics (old 584)
    assert row.chg_pct == 0.8  # fields the quote lacks are untouched


def test_error_quote_sets_error_overlays_nothing() -> None:
    row = apply_quote(_merged_yahoo(), {"error": "provider timeout"})
    assert row.error == "provider timeout"
    assert row.last == 5_234.0 and row.chg_pct == 0.8  # old 572-576: skip


def test_unchanged_error_keeps_row_identical() -> None:
    """Repaint suppression: the old screen only re-rendered on change."""
    errored = apply_quote(placeholder("HG25"), {"error": "timeout"})
    assert apply_quote(errored, {"error": "timeout"}) is errored


def test_empty_quote_leaves_row_untouched() -> None:
    row = _merged_yahoo()
    assert apply_quote(row, None) is row
    assert apply_quote(row, {}) is row


# ── merged/pending classification ─────────────────────────────────────


def test_placeholder_is_pending_until_served() -> None:
    assert is_pending(placeholder("X")) and not is_merged(placeholder("X"))
    thin = BoardRow(key="X", source="yahoo", chg_pct=0.5)  # any served field
    assert not is_pending(thin) and is_merged(thin)
    errored = apply_quote(placeholder("X"), {"error": "boom"})
    assert is_merged(errored)  # concrete errors count (old cache semantics)


# ── leg merge (old domains.py:537-556) ────────────────────────────────


def test_late_leg_never_wipes_merged_rows() -> None:
    standing = {"HG25": apply_quote(placeholder("HG25"), {"last": 5_310.0})}
    leg = {"HG25": BoardRow(key="HG25", source="sgx", last=5_000.0,
                            unit="SGD", curve=({"month": "DEC26",
                                                "settle": 5_100.0},))}
    merged = merge_leg(standing, leg, roster=["HG25"])
    assert merged["HG25"].last == 5_310.0  # standing quote data wins
    assert merged["HG25"].source == "sgx"  # scaffold refreshed from the leg
    assert merged["HG25"].unit == "SGD" and merged["HG25"].curve


def test_leg_fills_pending_placeholders() -> None:
    leg = {"FE26": BoardRow(key="FE26", source="sgx", last=18.4,
                            unit="USD")}
    merged = merge_leg({"FE26": placeholder("FE26")}, leg, roster=["FE26"])
    assert merged["FE26"].last == 18.4 and not is_pending(merged["FE26"])


def test_roster_symbols_without_rows_keep_pending() -> None:
    merged = merge_leg({}, {}, roster=["A", "B"])
    assert all(is_pending(row) for row in merged.values())
    assert set(merged) == {"A", "B"}


def test_board_rebuild_drops_keys_outside_leg_and_roster() -> None:
    """The old board was rebuilt from the landing result; rows neither in
    the leg nor on the roster do not survive the rebuild."""
    standing = {"OLD": _merged_yahoo("OLD"), "KEPT": _merged_yahoo("KEPT")}
    merged = merge_leg(standing, {}, roster=["KEPT"])
    assert set(merged) == {"KEPT"}
    assert merged["KEPT"].last == 5_234.0  # roster keeps merged standing data
