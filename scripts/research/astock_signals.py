"""A-share daily signal card — TopkDropout selection for MANUAL execution.

No quant account is required: after each close, run this to get the
rebalance card (SELL dropped names, BUY new names with 100-share lots,
HOLD the kept ones), place the orders by hand in the broker app, then
log the fills in the workstation's LEDGER so the book stays honest.

State: intel/astock/holdings.json (current names) and
intel/astock/selection-<date>.json (each card, for audit).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from services.config import Config  # noqa: E402

STATE = "intel/astock"


def holdings_path(state_root: str) -> Path:
    return Path(state_root) / STATE / "holdings.json"


def load_holdings(state_root: str) -> list[str]:
    path = holdings_path(state_root)
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("holdings", [])


def save_holdings(state_root: str, holdings: list[str]) -> None:
    path = holdings_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"holdings": holdings}, indent=1), encoding="utf-8")


PANEL_FRESH_HOURS = 12.0


def _reload(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cn_direct_panel(symbols: list[str], state_root: str) -> dict[str, pd.Series]:
    """Primary path: quant-python + akshare, CN-direct (no proxy, no 429).

    Reuses a fresh (<12h) panel file; otherwise respawns the fetcher and
    converts the JSON to a close series per symbol. Returns {} on any
    failure so the caller falls back to the quantkit/yfinance path."""
    import os
    import subprocess

    py = os.path.expanduser("~/.local/bin/quant-python")
    if not os.path.exists(py):
        return {}
    script = Path(__file__).with_name("astock_fetch.py")
    out = Path(state_root) / "intel" / "astock" / "panel-latest.json"
    try:
        cached = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else {}
    except (OSError, ValueError):
        cached = {}
    usable = bool(cached.get("panel")) and cached.get("asof")
    fresh = (usable
             and time.time() - out.stat().st_mtime < PANEL_FRESH_HOURS * 3600)
    if not fresh:
        try:
            proc = subprocess.run(
                [py, str(script), "--out", str(out)],
                input=json.dumps(symbols), capture_output=True, text=True,
                timeout=300)
            if proc.returncode != 0 or not out.is_file():
                return {}
        except (OSError, subprocess.TimeoutExpired):
            return {}
    payload = cached if fresh else _reload(out)
    if payload is None:
        return {}
    series: dict[str, pd.Series] = {}
    for symbol, rows in (payload.get("panel") or {}).items():
        if len(rows) >= 60:
            series[symbol] = pd.Series(
                [float(close) for _d, close in rows],
                index=pd.to_datetime([d for d, _c in rows])).sort_index()
    return series


def build_panel(config: Config, symbols: list[str], min_bars: int = 200,
                state_root: str = "") -> pd.DataFrame:
    """Fresh daily closes per symbol: CN-direct akshare via quant-python
    first, then the quantkit fetch, then the parquet cache as the last
    resort. Symbols that fail stay out."""
    if state_root:
        cn = _cn_direct_panel(symbols, state_root)
        if len(cn) >= max(3, len(symbols) // 2):
            frame = pd.DataFrame(cn)
            return frame[sorted(frame.columns)]  # column order, not row reindex
    series: dict[str, pd.Series] = {}
    try:
        from quantkit.portfolio import fetch_price_panel

        panel = fetch_price_panel(symbols, start="2025-06-01",
                                  data_dir=str(config.data_dir))
        if panel is not None and not panel.empty:
            for column in panel.columns:
                s = panel[column].dropna()
                if len(s) >= min_bars:
                    series[str(column).upper()] = s
    except Exception:
        pass
    for sym, s in series.items():
        s.index = pd.to_datetime(s.index)
    return pd.DataFrame(series).sort_index()


def lots(shares: int) -> int:
    """A-share board lot: orders are in 100-share multiples."""
    return max(0, int(shares) // 100 * 100)


def signal(config: Config, *, capital: float, topk: int = 5, n_drop: int = 1,
           mode: str = "mean_reversion", mom_lookback: int = 60,
           symbols: list[str] | None = None, state_root: str | None = None
           ) -> dict:
    from quantkit.selection import score_universe, select_topk_dropout

    state_root = state_root or (config.state_root or str(Path.home() / ".local/share/stammtisch"))
    universe = symbols or re.findall(
        r"auto_(.+?)_1d", " ".join(p.name for p in
                                   (Path(config.data_dir) / "cache").glob("*.parquet")))
    universe = sorted({u.upper() for u in universe})
    prices = build_panel(config, universe, state_root=state_root)
    px = prices.ffill().dropna()  # common window: rows, not columns
    current = load_holdings(state_root)
    scores = score_universe(px, mode=mode, mom_lookback=mom_lookback)
    selected, weights = select_topk_dropout(scores, current, topk, n_drop)
    kept = [s for s in selected if s in current]
    sells = [s for s in current if s not in selected]
    buys = [s for s in selected if s not in current]
    ranked = scores.sort_values("score", ascending=False).head(12)
    card = {"asof": str(px.index[-1].date()), "mode": mode,
            "panel": f"{px.shape[1]} symbols x {px.shape[0]} bars",
            "ranked": [{"symbol": sym, "score": round(float(row["score"]), 4),
                        "close": round(float(row.get("close", 0) or 0), 2)}
                       for sym, row in ranked.iterrows()],
            "holdings_before": current, "holdings_after": selected,
            "sells": sells, "buys": buys, "kept": kept, "weights": {},
            "capital": capital, "topk": topk, "n_drop": n_drop,
            "factors": {}, "spark": {}}
    factor_cols = [c for c in (scores.columns if hasattr(scores, "columns") else [])
                   if c not in ("asof",)]
    for sym in ranked.index:
        if sym in scores.index:
            card["factors"][sym] = {
                col: (round(float(scores.loc[sym, col]), 4)
                      if pd.api.types.is_number(scores.loc[sym, col])
                      else str(scores.loc[sym, col]))
                for col in factor_cols}
        if sym in px.columns:
            tail = [float(v) for v in px[sym].dropna().tail(30)]
            card["spark"][sym] = tail
    for sym in selected:
        shares = lots(capital * float(weights.get(sym, 0)) / px[sym].iloc[-1])
        if shares:
            card["weights"][sym] = {
                "target_weight": round(float(weights.get(sym, 0)), 4),
                "shares": shares,
                "ref_price": round(float(px[sym].iloc[-1]), 2)}
    return card


def main() -> None:
    parser = argparse.ArgumentParser(description="A-share daily TopkDropout signal card")
    parser.add_argument("--capital", type=float, default=1000000)
    parser.add_argument("--topk", type=int, default=5)
    parser.add_argument("--n-drop", type=int, default=1)
    parser.add_argument("--mode", default="mean_reversion",
                        choices=["mean_reversion", "momentum", "composite"])
    parser.add_argument("--confirm", action="store_true",
                        help="apply the card as the new holdings")
    args = parser.parse_args()
    config = Config()
    card = signal(config, capital=args.capital, topk=args.topk,
                  n_drop=args.n_drop, mode=args.mode)
    state_root = config.state_root or str(Path.home() / ".local/share/stammtisch")
    out = Path(state_root) / STATE
    out.mkdir(parents=True, exist_ok=True)
    (out / f"selection-{card['asof']}.json").write_text(
        json.dumps(card, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"A-SHARE SIGNAL CARD  {card['asof']}  ({card['mode']}, {card['panel']})")
    print(f"  SELL (手动卖出): {', '.join(card['sells']) or '—'}")
    print(f"  BUY  (手动买入): " + ", ".join(
        f"{sym} {lot['shares']}股 @≈{lot['ref_price']}"
        for sym, lot in card["weights"].items() if sym in card["buys"]) or "—")
    print(f"  HOLD (继续持有): {', '.join(card['kept']) or '—'}")
    print(f"  卡片已存档: {out / f'selection-{card['asof']}.json'}")
    if args.confirm:
        save_holdings(state_root, card["holdings_after"])
        print(f"  holdings updated -> {holdings_path(state_root)}")
    print("  成交后请用 LEDGER 屏或 stammtisch 台账登记实际成交，保持账实一致。")


if __name__ == "__main__":
    main()
