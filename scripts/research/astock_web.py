"""Local web page for the A-share signal card (127.0.0.1 only).

GET /         -> styled HTML signal card (BUY / SELL / HOLD + factors)
GET /api/card -> the same payload as JSON

A background thread regenerates the card every 30 minutes through the
quantkit selection stack; requests only read the cached payload.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PORT = 8787
REFRESH_SECONDS = 1800
LOCK = threading.Lock()
CARD: dict = {"error": "first refresh in progress…"}


def refresh_loop() -> None:
    global CARD
    from tui.config import Config

    tree = str(Config().quantkit_path or "").strip()
    if tree:
        for name in [m for m in list(sys.modules)
                     if m == "quantkit" or m.startswith("quantkit.")]:
            sys.modules.pop(name, None)
        sys.path.insert(0, tree)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from astock_signals import signal

    while True:
        try:
            with LOCK:
                CARD = signal(Config(), capital=float(
                    os.environ.get("ASTOCK_CAPITAL", "100000")))
                CARD["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as exc:
            with LOCK:
                CARD = {"error": f"refresh failed: {exc}"}
        time.sleep(REFRESH_SECONDS)


def spark_svg(values: list[float], width: int = 120, height: int = 28) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = width / (len(values) - 1)
    pts = " ".join(
        f"{i * step:.1f},{height - (v - lo) / span * (height - 4) - 2:.1f}"
        for i, v in enumerate(values))
    color = "#66bb6a" if values[-1] >= values[0] else "#ef5350"
    return (f"<svg width='{width}' height='{height}' viewBox='0 0 {width} {height}'>"
            f"<polyline points='{pts}' fill='none' stroke='{color}' stroke-width='1.5'/>"
            f"</svg>")


def render(card: dict) -> str:
    if "error" in card and "weights" not in card:
        return (f"<!doctype html><meta charset=utf-8><body style='background:#06080a;"
                f"color:#eee;font-family:monospace'><h2>{card['error']}</h2>")

    def money(value: float) -> str:
        return f"{value:,.2f}"

    def buy_rows(weights: dict, factors: dict, sparks: dict) -> str:
        out = ""
        for sym, lot in weights.items():
            factor = factors.get(sym, {})
            chips = "".join(
                f"<span class='chip'>{k} {v}</span>" for k, v in factor.items())
            trend = spark_svg(sparks.get(sym, []))
            out += (f"<div class='card'><div class='cardhead'>"
                    f"<span class='sym'>{sym}</span>"
                    f"<span class='shares'>{lot['shares']} 股</span>"
                    f"<span class='price'>≈ {money(lot['ref_price'])}</span>"
                    f"<span class='w'>{lot['target_weight']:.1%}</span>"
                    f"<span class='amt'>≈ {money(lot['shares'] * lot['ref_price'])}</span>"
                    f"</div>{trend}<div class='chips'>{chips}</div></div>")
        return out or "<div class='empty'>—</div>"

    def simple_rows(symbols: list[str], verb: str, empty: str) -> str:
        if not symbols:
            return f"<div class='empty'>{empty}</div>"
        return "".join(
            f"<div class='card simple'><span class='sym'>{s}</span>"
            f"<span class='shares'>{verb}</span></div>" for s in symbols)

    buys = buy_rows(card.get("weights", {}), card.get("factors", {}),
                    card.get("spark", {}))
    sells = simple_rows(card.get("sells", []), "全部卖出", "无")
    kept = simple_rows(card.get("kept", []), "继续持有", "无")
    deployed = sum(lot["shares"] * lot["ref_price"]
                   for lot in card.get("weights", {}).values())
    return f"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta http-equiv=refresh content="{REFRESH_SECONDS // 2}">
<title>A股 TopkDropout 信号卡</title><style>
* {{ box-sizing: border-box; margin: 0; }}
body {{ background: #06080a; color: #c8ccd0; font-family: 'SF Mono', Consolas, monospace; padding: 18px 22px; }}
header {{ display: flex; justify-content: space-between; align-items: baseline; border-bottom: 1px solid #1d2a33; padding-bottom: 10px; margin-bottom: 14px; }}
h1 {{ font-size: 17px; color: #fff; letter-spacing: 1px; }}
.meta {{ color: #5a6a75; font-size: 12px; text-align: right; line-height: 1.5; }}
.tiles {{ display: flex; gap: 10px; margin-bottom: 16px; }}
.tile {{ background: #0c1216; border: 1px solid #1d2a33; border-radius: 4px; padding: 8px 14px; min-width: 110px; }}
.tile .k {{ color: #5a6a75; font-size: 11px; }} .tile .v {{ color: #fff; font-size: 16px; margin-top: 3px; }}
h2 {{ font-size: 13px; color: #7d8b96; letter-spacing: 2px; margin: 16px 0 8px; }}
h2.buy {{ color: #66bb6a; }} h2.sell {{ color: #ef5350; }} h2.keep {{ color: #4fc3f7; }}
.card {{ background: #0c1216; border: 1px solid #1d2a33; border-left: 3px solid #66bb6a; border-radius: 4px; padding: 10px 12px; margin-bottom: 8px; display: flex; flex-wrap: wrap; gap: 14px; align-items: center; }}
.card.simple {{ border-left-color: #4fc3f7; justify-content: space-between; }}
.sym {{ color: #fff; font-size: 15px; font-weight: bold; min-width: 110px; }}
.shares {{ color: #ffd54f; min-width: 80px; }} .price {{ color: #a8b4bc; }}
.w {{ color: #4fc3f7; }} .amt {{ color: #8899a6; }}
.chips {{ display: flex; gap: 6px; flex-wrap: wrap; }}
.chip {{ background: #101a20; border: 1px solid #1d2a33; border-radius: 3px; color: #7d8b96; font-size: 11px; padding: 2px 7px; }}
.empty {{ color: #3d4a52; padding: 6px 0; }}
footer {{ margin-top: 20px; color: #3d4a52; font-size: 11px; line-height: 1.7; border-top: 1px solid #1d2a33; padding-top: 10px; }}
</style></head><body>
<header><h1>A股 TopkDropout 信号卡 · 均值回归打分</h1>
<div class=meta>面板 {card.get('panel')}<br>topk {card.get('topk')} / n_drop {card.get('n_drop')} · 生成 {card.get('generated_at')}</div></header>
<div class=tiles>
<div class=tile><div class=k>部署资金</div><div class=v>{money(deployed)}</div></div>
<div class=tile><div class=k>目标持仓</div><div class=v>{len(card.get('holdings_after', []))}</div></div>
<div class=tile><div class=k>本期换入</div><div class=v>{len(card.get('buys', []))}</div></div>
<div class=tile><div class=k>本期换出</div><div class=v>{len(card.get('sells', []))}</div></div>
</div>
<h2 class=buy>BUY — 手动买入（100 股整手）</h2>{buys}
<h2 class=sell>SELL — 手动卖出</h2>{sells}
<h2 class=keep>HOLD — 继续持有（带内保留）</h2>{kept}
<footer>仅参考信号，非投资建议 · 数据为日频收盘（免费源，可能有延迟）· 手动下单后在 LEDGER 登记实际成交<br>
持仓带规则：现持仓排名仍在 topk+n_drop 带内则保留，每月最多自然换出 n_drop 只 · 卡片存档 intel/astock/</footer>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with LOCK:
            payload = (json.dumps(CARD, ensure_ascii=False, default=str)
                       if self.path == "/api/card" else render(CARD))
        ctype = ("application/json" if self.path == "/api/card"
                 else "text/html") + "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=refresh_loop, daemon=True).start()
    time.sleep(1)
    print(f"A-share signal card: http://127.0.0.1:{PORT}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
