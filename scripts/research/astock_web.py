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


def spark_svg(values: list[float], width: int = 110, height: int = 26) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    step = width / (len(values) - 1)
    pts = " ".join(
        f"{i * step:.1f},{height - (v - lo) / span * (height - 4) - 2:.1f}"
        for i, v in enumerate(values))
    color = "#e8e8e8" if values[-1] >= values[0] else "#6e6e6e"
    return (f"<svg width='{width}' height='{height}' viewBox='0 0 {width} {height}'>"
            f"<polyline points='{pts}' fill='none' stroke='{color}' stroke-width='1.2'/>"
            f"</svg>")


def render(card: dict) -> str:
    if "error" in card and "weights" not in card:
        return (f"<!doctype html><meta charset=utf-8><body style='background:#000;"
                f"color:#e8e8e8;font-family:Georgia,serif'><h2>{card['error']}</h2>")

    def money(value: float) -> str:
        return f"{value:,.2f}"

    weights = card.get("weights", {})
    factors = card.get("factors", {})
    sparks = card.get("spark", {})
    ranked = card.get("ranked", [])
    deployed = sum(lot["shares"] * lot["ref_price"] for lot in weights.values())

    buy_cards = ""
    for sym, lot in weights.items():
        factor = factors.get(sym, {})
        rows = "".join(
            f"<div class='frow'><span>{k}</span><span>"
            f"{('+' if isinstance(v, (int, float)) and v >= 0 else '')}"
            f"{v if isinstance(v, (int, float)) and abs(v) < 10 else v}</span></div>"
            for k, v in factor.items())
        buy_cards += (
            f"<div class='name'><div class='sym'>{sym}</div>"
            f"<div class='tag'>BUY · {lot['target_weight']:.0%}</div></div>"
            f"<div class='num'>{lot['shares']:,} 股</div>"
            f"<div class='num'>{money(lot['ref_price'])}</div>"
            f"<div class='num'>{money(lot['shares'] * lot['ref_price'])}</div>"
            f"<div class='spark'>{spark_svg(sparks.get(sym, []))}</div>"
            f"<div class='factors'>{rows}</div>")

    sell_rows = "".join(
        f"<div class='name'><div class='sym'>{s}</div>"
        f"<div class='tag'>EXIT</div></div>" for s in card.get("sells", [])) or         "<div class='none'>—</div>"
    keep_rows = "".join(
        f"<div class='name'><div class='sym'>{s}</div>"
        f"<div class='tag'>HOLD</div></div>" for s in card.get("kept", [])) or         "<div class='none'>—</div>"
    board = "".join(
        f"<div class='brow'><span class='rk'>{i + 1:02d}</span>"
        f"<span class='rsym'>{r['symbol']}</span>"
        f"<span class='rsc'>{r['score']:+.3f}</span>"
        f"<span class='rpc'>{money(r['close'])}</span>"
        f"<span class='rbadge'>{'IN PORTFOLIO' if r['symbol'] in card.get('holdings_after', []) else ''}</span></div>"
        for i, r in enumerate(ranked))

    return f"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta http-equiv=refresh content="{REFRESH_SECONDS // 2}">
<title>A股 TopkDropout 信号卡</title><style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: #000; color: #e8e8e8; font-family: 'Helvetica Neue', 'PingFang SC', sans-serif;
        padding: 34px 48px; -webkit-font-smoothing: antialiased; }}
.mono {{ font-variant-numeric: tabular-nums; }}
header {{ border-bottom: 1px solid #262626; padding-bottom: 18px; margin-bottom: 6px; }}
.eyebrow {{ font-size: 10px; letter-spacing: 4px; color: #7a7a7a; text-transform: uppercase; }}
h1 {{ font-size: 26px; font-weight: 400; color: #fff; margin: 6px 0 2px; }}
.sub {{ color: #7a7a7a; font-size: 12px; }}
.strip {{ display: flex; gap: 0; border: 1px solid #262626; border-left: none; margin: 22px 0 30px; }}
.cell {{ flex: 1; padding: 14px 20px; border-left: 1px solid #262626; }}
.cell:first-child {{ border-left: none; }}
.cell .k {{ font-size: 10px; letter-spacing: 2.5px; color: #7a7a7a; text-transform: uppercase; }}
.cell .v {{ font-size: 22px; font-weight: 300; color: #fff; margin-top: 6px; font-variant-numeric: tabular-nums; }}
h2 {{ font-size: 11px; letter-spacing: 4px; color: #8a8a8a; text-transform: uppercase;
      font-weight: 400; margin: 34px 0 14px; border-bottom: 1px solid #1a1a1a; padding-bottom: 8px; }}
.ledger {{ display: grid; grid-template-columns: 2fr 1fr 1fr 1fr 130px; gap: 0 18px; }}
.ledger .head, .ledger > .row {{ display: contents; }}
.row {{ border-bottom: 1px solid #141414; padding: 12px 0; align-items: center; }}
.sym {{ font-size: 17px; color: #fff; font-weight: 500; }}
.tag {{ font-size: 10px; letter-spacing: 2px; color: #7a7a7a; margin-top: 3px; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; font-size: 15px; color: #e8e8e8; }}
.spark {{ text-align: right; }}
.factors {{ grid-column: 1 / -1; display: flex; gap: 26px; padding-top: 10px; }}
.frow {{ display: flex; gap: 8px; font-size: 11px; color: #7a7a7a; font-variant-numeric: tabular-nums; }}
.frow span:first-child {{ color: #555555; }}
.none {{ color: #4a4a4a; padding: 10px 0; font-style: italic; }}
.board {{ font-variant-numeric: tabular-nums; }}
.brow {{ display: flex; gap: 24px; padding: 7px 0; border-bottom: 1px solid #101010; font-size: 13px; }}
.rk {{ color: #555; width: 26px; }} .rsym {{ color: #e8e8e8; width: 110px; }}
.rsc {{ color: #b8b8b8; width: 90px; }} .rpc {{ color: #8a8a8a; width: 90px; }}
.rbadge {{ color: #d0d0d0; font-size: 10px; letter-spacing: 2px; }}
footer {{ margin-top: 44px; color: #4a4a4a; font-size: 11px; line-height: 1.8;
          border-top: 1px solid #1a1a1a; padding-top: 14px; }}
footer b {{ color: #7a7a7a; font-weight: 400; }}
</style></head><body>
<header><div class=eyebrow>STAMMTISCH — A-SHARE SELECTION · TOPKDROPOUT({card.get('topk')},{card.get('n_drop')})</div>
<h1>信号卡 · 均值回归打分</h1>
<div class=sub>面板 {card.get('panel')} · 数据截至 {card.get('asof')} · 生成于 {card.get('generated_at')}（每 30 分钟重算）</div></header>
<div class=strip>
<div class=cell><div class=k>部署资金</div><div class=v>{money(deployed)}</div></div>
<div class=cell><div class=k>基数本金</div><div class=v>{money(float(card.get('capital') or 0))}</div></div>
<div class=cell><div class=k>目标持仓</div><div class=v>{len(card.get('holdings_after', []))} / {card.get('topk')}</div></div>
<div class=cell><div class=k>本期换入 / 换出</div><div class=v>{len(card.get('buys', []))} / {len(card.get('sells', []))}</div></div>
</div>
<h2>BUY — 建仓指令（100 股整手）</h2>
<div class=ledger>
<div class=head><div class=row><div class=sym style="color:#7a7a7a;font-size:10px;letter-spacing:2px">SYMBOL</div><div class=num style="color:#7a7a7a;font-size:10px;letter-spacing:2px">股数</div><div class=num style="color:#7a7a7a;font-size:10px;letter-spacing:2px">参考价</div><div class=num style="color:#7a7a7a;font-size:10px;letter-spacing:2px">金额</div><div style="color:#7a7a7a;font-size:10px;letter-spacing:2px">30D</div></div></div>
{buy_cards}
</div>
<h2>SELL — 清仓指令</h2>{sell_rows}
<h2>HOLD — 带内保留</h2>{keep_rows}
<h2>UNIVERSE LEADERBOARD — 全面板评分前 12</h2>
<div class=board>{board}</div>
<footer><b>纪律</b> 现持仓排名在 topk+n_drop 带内保留 · 每月最多自然换出 {card.get('n_drop')} 只 ·
卡片存档 intel/astock/ · 成交后于 LEDGER 登记实际成交<br>
<b>声明</b> 仅参考信号，非投资建议 · 日频收盘免费源可能有延迟 · 过往表现不代表未来</footer>
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
