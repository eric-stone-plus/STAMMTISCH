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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd

from tui.config import Config  # noqa: E402

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


def build_panel(config: Config, symbols: list[str], min_bars: int = 200) -> pd.DataFrame:
    """Fresh daily closes per symbol: quantkit fetch (cache-first), then
    the parquet cache as fallback. Symbols that fail stay out."""
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
    prices = build_panel(config, universe)
    px = prices.ffill().dropna(axis=1, how="any")
    current = load_holdings(state_root)
    scores = score_universe(px, mode=mode, mom_lookback=mom_lookback)
    selected, weights = select_topk_dropout(scores, current, topk, n_drop)
    kept = [s for s in selected if s in current]
    sells = [s for s in current if s not in selected]
    buys = [s for s in selected if s not in current]
    card = {"asof": str(px.index[-1].date()), "mode": mode,
            "panel": f"{px.shape[1]} symbols x {px.shape[0]} bars",
            "holdings_before": current, "holdings_after": selected,
            "sells": sells, "buys": buys, "kept": kept, "weights": {}}
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
    parser.add_argument("--capital", type=float, default=100000)
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
