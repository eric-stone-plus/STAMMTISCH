"""A-share signal card web page — interactive, localhost only.

GET  /              -> styled HTML signal card (BUY/SELL/HOLD + leaderboard
                       + clickable candlestick drawer)
GET  /api/card      -> the payload as JSON
GET  /api/candles?symbol=X[&days=N] -> OHLC candles (verified quantkit
                       cache first, yfinance fallback)
POST /api/holdings  -> {"action": "add"|"remove", "symbol": "..."} updates
                       the manual portfolio band (holdings.json)
POST /api/refresh   -> force an immediate recompute

A background thread regenerates the card every 30 minutes through the
quantkit selection stack; requests only read the cached payload unless
a refresh is forced.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tui.config import Config  # noqa: E402

PORT = 8787
REFRESH_SECONDS = 1800
LOCK = threading.Lock()
CARD: dict = {"error": "first refresh in progress…"}
REFRESH_NOW = threading.Event()


def refresh_loop() -> None:
    global CARD
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
                    os.environ.get("ASTOCK_CAPITAL", "1000000")))
                CARD["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as exc:
            with LOCK:
                CARD = {"error": f"refresh failed: {exc}"}
        REFRESH_NOW.wait(REFRESH_SECONDS)
        REFRESH_NOW.clear()


def candles_payload(symbol: str, days: int = 120) -> dict:
    """OHLC candles: verified quantkit cache first, yfinance fallback."""
    symbol = symbol.strip().upper()
    candles: list[dict] = []
    cache_dir = Path(Config().data_dir) / "cache"
    files = sorted(cache_dir.glob(f"*auto_{symbol}_1d*.parquet"))
    if files:
        df = pd.read_parquet(files[-1]).tail(days)
        df.columns = [str(c).lower() for c in df.columns]
        if {"open", "high", "low", "close"}.issubset(df.columns):
            candles = [{"time": str(i)[:10], "open": float(r["open"]),
                        "high": float(r["high"]), "low": float(r["low"]),
                        "close": float(r["close"]),
                        "volume": float(r.get("volume", 0) or 0)}
                       for i, r in df.iterrows()]
    if not candles:
        try:
            import yfinance as yf
            proxy = Config().egress_proxy_url or None
            if proxy:
                yf.config.network.proxy = proxy
            df = yf.download(symbol, period="6mo", progress=False,
                             auto_adjust=True)
            if df is not None and not df.empty:
                candles = [{"time": str(i)[:10], "open": float(r["Open"]),
                            "high": float(r["High"]), "low": float(r["Low"]),
                            "close": float(r["Close"]),
                            "volume": float(r["Volume"])}
                           for i, r in df.tail(days).iterrows()]
        except Exception:
            pass
    if not candles:
        raise RuntimeError(f"no candles for {symbol}")
    return {"symbol": symbol, "candles": candles[-days:]}


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


TEMPLATE = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<script src="/static/lightweight-charts.standalone.production.js"></script>
<title>A股 TopkDropout 信号卡</title><style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #000; color: #e8e8e8; font-family: 'Helvetica Neue', 'PingFang SC', sans-serif;
       padding: 34px 48px; -webkit-font-smoothing: antialiased; }
header { display: flex; justify-content: space-between; align-items: baseline;
         border-bottom: 1px solid #262626; padding-bottom: 18px; margin-bottom: 6px; }
.eyebrow { font-size: 10px; letter-spacing: 4px; color: #7a7a7a; text-transform: uppercase; }
h1 { font-size: 26px; font-weight: 400; color: #fff; margin: 6px 0 2px; }
.sub { color: #7a7a7a; font-size: 12px; text-align: right; line-height: 1.6; }
button { background: #111; border: 1px solid #333; color: #c8c8c8; font-size: 11px;
         padding: 4px 10px; cursor: pointer; letter-spacing: 1px; }
button:hover { border-color: #666; color: #fff; }
.strip { display: flex; border: 1px solid #262626; border-left: none; margin: 22px 0 30px; }
.cell { flex: 1; padding: 14px 20px; border-left: 1px solid #262626; }
.cell:first-child { border-left: none; }
.cell .k { font-size: 10px; letter-spacing: 2.5px; color: #7a7a7a; text-transform: uppercase; }
.cell .v { font-size: 22px; font-weight: 300; color: #fff; margin-top: 6px;
           font-variant-numeric: tabular-nums; }
h2 { font-size: 11px; letter-spacing: 4px; color: #8a8a8a; text-transform: uppercase;
     font-weight: 400; margin: 34px 0 14px; border-bottom: 1px solid #1a1a1a; padding-bottom: 8px; }
.ledger { display: grid; grid-template-columns: 2fr 1fr 1fr 1fr 130px; gap: 0 18px; }
.sym { font-size: 17px; color: #fff; font-weight: 500; }
.tag { font-size: 10px; letter-spacing: 2px; color: #7a7a7a; margin-top: 3px; }
.num { text-align: right; font-variant-numeric: tabular-nums; font-size: 15px; }
.factors { grid-column: 1 / -1; display: flex; gap: 26px; padding-top: 10px; }
.frow { display: flex; gap: 8px; font-size: 11px; color: #7a7a7a;
        font-variant-numeric: tabular-nums; }
.frow span:first-child { color: #555; }
.none { color: #4a4a4a; padding: 10px 0; font-style: italic; }
.board { font-variant-numeric: tabular-nums; }
.brow { display: flex; gap: 24px; padding: 7px 0; border-bottom: 1px solid #101010;
        font-size: 13px; align-items: center; transition: background .15s; }
.brow:hover { background: #0d0d0d; cursor: pointer; }
.rk { color: #555; width: 26px; } .rsym { color: #e8e8e8; width: 110px; }
.rsc { color: #b8b8b8; width: 90px; } .rpc { color: #8a8a8a; width: 90px; }
.rbadge { color: #d0d0d0; font-size: 10px; letter-spacing: 2px; width: 130px; }
.card { background: #0a0a0a; border: 1px solid #1d2a33; border-left: 2px solid #3a3a3a;
        border-radius: 2px; padding: 12px 14px; margin-bottom: 8px;
        display: flex; flex-wrap: wrap; gap: 16px; align-items: center;
        transition: border-color .15s, background .15s, transform .15s;
        cursor: pointer; }
.card:hover { border-left-color: #fff; background: #0d0d0d; transform: translateX(3px); }
.card:hover .hint { color: #e8e8e8; }
.name { min-width: 120px; }
.hint { color: #4a4a4a; font-size: 10px; margin-left: auto; letter-spacing: 1px; }
.chips { display: flex; gap: 6px; flex-wrap: wrap; }
.chip { background: #0d0d0d; border: 1px solid #1d2a33; border-radius: 2px;
        color: #7d8b96; font-size: 11px; padding: 2px 7px; }
#drawer { display: none; border: 1px solid #262626; background: #000; padding: 18px;
          margin-top: 14px; }
#drawer h3 { color: #fff; font-weight: 500; margin-bottom: 12px; }
#chartbox { height: 340px; margin-bottom: 12px; }
#status { color: #ffd54f; font-size: 12px; margin-left: 12px; }
footer { margin-top: 44px; color: #4a4a4a; font-size: 11px; line-height: 1.8;
         border-top: 1px solid #1a1a1a; padding-top: 14px; }
footer b { color: #7a7a7a; font-weight: 400; }
</style></head><body>
<header><div class=eyebrow>STAMMTISCH — A-SHARE SELECTION · TOPKDROPOPOUT(__TOPK__,__NDROP__)</div>
<div class=sub style="text-align:right">面板 __PANEL__ · 数据截至 __ASOF__<br>
生成于 __GENERATED__<span id=status></span></div></header>
<div class=strip>
<div class=cell><div class=k>部署资金</div><div class=v>__DEPLOYED__</div></div>
<div class=cell><div class=k>基数本金</div><div class=v>__CAPITAL__</div></div>
<div class=cell><div class=k>目标持仓</div><div class=v>__NHOLD__ / __TOPK__</div></div>
<div class=cell><div class=k>本期换入 / 换出</div><div class=v>__NBUYS__ / __NSELLS__</div></div>
<div class=cell style="display:flex;align-items:center"><button onclick="forceRefresh()">立即重算</button></div>
</div>
<h2>BUY — 建仓指令（100 股整手 · 点击看 K 线）</h2>
__BUYCARDS__
<h2>SELL — 清仓指令</h2>__SELLS__
<h2>HOLD — 带内保留（点击看 K 线）</h2>__KEPT__
<h2>UNIVERSE LEADERBOARD — 全面板评分前 12</h2>
<div class=board>__BOARD__</div>
<div id=drawer></div>
<footer><b>纪律</b> 现持仓排名在 topk+n_drop 带内保留 · 每月最多自然换出 __NDROP__ 只 ·
加入/移出按钮直接修改手动组合带（holdings.json），下张卡片按新带计算 ·
卡片存档 intel/astock/ · 成交后于 LEDGER 登记实际成交<br>
<b>声明</b> 仅参考信号，非投资建议 · 日频收盘免费源可能有延迟 · 过往表现不代表未来</footer>
<script>
const DATA = __DATA__;
function showDrawer(sym) {
  const d = document.getElementById('drawer');
  const f = DATA.factors[sym] || {};
  d.innerHTML = '<h3>' + sym + ' — 日K · 因子明细</h3><div id=chartbox></div><div class=chips>'
    + Object.entries(f).map(([k, v]) => '<span class=chip>' + k + ' ' + v + '</span>').join('')
    + '</div>';
  d.style.display = 'block';
  fetch('/api/candles?symbol=' + sym + '&days=120').then(r => r.json()).then(j => {
    const box = document.getElementById('chartbox');
    if (!j.candles || !j.candles.length) {
      box.innerHTML = '<div class=none>无K线数据（缓存与免费源均无）</div>';
      return;
    }
    const candles = j.candles;
    if (window.LightweightCharts) {
      box.innerHTML = '';
      const chart = LightweightCharts.createChart(box, {
        height: 340,
        layout: { background: '#000', textColor: '#8a8a8a' },
        grid: { vertLines: { color: '#0e0e0e' }, horzLines: { color: '#0e0e0e' } },
        rightPriceScale: { borderColor: '#262626' },
        timeScale: { borderColor: '#262626' } });
      const add = chart.addCandlestickSeries
        ? chart.addCandlestickSeries.bind(chart)
        : (o) => chart.addSeries(LightweightCharts.CandlestickSeries, o);
      add({ upColor: '#2a2a2a', downColor: '#000', borderUpColor: '#e8e8e8',
            borderDownColor: '#6e6e6e', wickUpColor: '#e8e8e8',
            wickDownColor: '#6e6e6e' })
        .setData(candles.map(c => ({ time: c.time, open: c.open, high: c.high,
                                     low: c.low, close: c.close })));
      chart.timeScale().fitContent();
    } else {
      const view = candles.slice(-60);
      const lo = Math.min(...view.map(c => c.low));
      const hi = Math.max(...view.map(c => c.high));
      const span = (hi - lo) || 1;
      const step = 560 / view.length;
      let svg = '<svg width="560" height="260">';
      view.forEach((c, i) => {
        const x = i * step + step / 2;
        const y = v => 255 - (v - lo) / span * 250;
        const up = c.close >= c.open;
        const top = y(Math.max(c.open, c.close));
        const bot = y(Math.min(c.open, c.close));
        svg += '<line x1="' + x + '" y1="' + y(c.high) + '" x2="' + x + '" y2="'
             + y(c.low) + '" stroke="' + (up ? '#e8e8e8' : '#6e6e6e')
             + '" stroke-width="1"/>';
        svg += '<rect x="' + (x - 1.6) + '" y="' + top + '" width="3.2" height="'
             + Math.max(bot - top, 1) + '" fill="'
             + (up ? '#e8e8e8' : '#6e6e6e') + '"/>';
      });
      svg += '</svg>';
      box.innerHTML = svg;
    }
    const chips = Object.entries(DATA.factors[sym] || {}).map(([k, v]) =>
      '<span class=chip>' + k + ' ' + v + '</span>').join('');
    box.insertAdjacentHTML('afterend', '<div class=chips>' + chips + '</div>');
  }).catch(() => {
    document.getElementById('chartbox').innerHTML = '<div class=none>K线加载失败</div>';
  });
}
function editHolding(symbol, add) {
  fetch('/api/holdings', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: add ? 'add' : 'remove', symbol})})
    .then(r => r.json())
    .then(j => { document.getElementById('status').textContent = j.status;
                 setTimeout(() => location.reload(), 800); });
}
function forceRefresh() {
  document.getElementById('status').textContent = '重算中（约 2 分钟）…';
  fetch('/api/refresh', {method: 'POST'})
    .then(r => r.json())
    .then(j => { document.getElementById('status').textContent = j.status;
                 setTimeout(() => location.reload(), 800); });
}
document.querySelectorAll('.brow').forEach(row =>
  row.addEventListener('click', e => {
    if (e.target.tagName === 'BUTTON') return;
    showDrawer(row.dataset.sym);
  }));
</script></body></html>"""


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
    holdings_after = card.get("holdings_after", [])
    deployed = sum(lot["shares"] * lot["ref_price"] for lot in weights.values())

    buy_cards = ""
    for sym, lot in weights.items():
        factor_rows = "".join(
            f"<div class='frow'><span>{k}</span><span>{v}</span></div>"
            for k, v in factors.get(sym, {}).items())
        buy_cards += (
            f"<div class='name'><div class='sym'>{sym}</div>"
            f"<div class='tag'>BUY · {lot['target_weight']:.0%}</div></div>"
            f"<div class='num'>{lot['shares']:,} 股</div>"
            f"<div class='num'>{money(lot['ref_price'])}</div>"
            f"<div class='num'>{money(lot['shares'] * lot['ref_price'])}</div>"
            f"<div class='spark'>{spark_svg(sparks.get(sym, []))}</div>"
            f"<div class='factors'>{factor_rows}</div>"
            f"<div class='hint' onclick='openChart(&quot;{sym}&quot;)'>"
            f"点击看 K 线 →</div>")
    sell_rows = "".join(
        f"<div class='name'><div class='sym'>{s}</div>"
        f"<div class='tag'>EXIT</div></div>" for s in card.get("sells", [])) or         "<div class='none'>—</div>"
    keep_rows = "".join(
        f"<div class='name' style='cursor:pointer' onclick='openChart(&quot;{s}&quot;)'>"
        f"<div class='sym'>{s}</div>"
        f"<div class='tag'>HOLD · 点击看K线</div></div>"
        for s in card.get("kept", [])) or "<div class='none'>—</div>"
    board = "".join(
        f"<div class='brow' data-sym='{r['symbol']}' "
        f"onclick='showDrawer(&quot;{r['symbol']}&quot;)'>"
        f"<span class='rk'>{i + 1:02d}</span>"
        f"<span class='rsym'>{r['symbol']}</span>"
        f"<span class='rsc'>{r['score']:+.3f}</span>"
        f"<span class='rpc'>{money(r['close'])}</span>"
        f"<span class='rbadge'>{'IN PORTFOLIO' if r['symbol'] in holdings_after else ''}</span>"
        f"<span class='act'><button onclick='event.stopPropagation();"
        f"editHolding(&quot;{r['symbol']}&quot;, "
        f"{'true' if r['symbol'] in holdings_after else 'false'})'>"
        f"{'移出组合' if r['symbol'] in holdings_after else '加入组合'}</button></span></div>"
        for i, r in enumerate(ranked))
    data_json = json.dumps({"factors": factors, "spark": sparks,
                            "holdings": holdings_after}, ensure_ascii=False)
    out = TEMPLATE
    for token, value in (
        ("__TOPK__", str(card.get("topk", ""))),
        ("__NDROP__", str(card.get("n_drop", ""))),
        ("__PANEL__", card.get("panel", "")),
        ("__ASOF__", card.get("asof", "")),
        ("__GENERATED__", card.get("generated_at", "")),
        ("__DEPLOYED__", money(deployed)),
        ("__CAPITAL__", money(float(card.get("capital") or 0))),
        ("__NHOLD__", str(len(holdings_after))),
        ("__NBUYS__", str(len(card.get("buys", [])))),
        ("__NSELLS__", str(len(card.get("sells", [])))),
        ("__BUYCARDS__", buy_cards),
        ("__SELLS__", sell_rows),
        ("__KEPT__", keep_rows),
        ("__BOARD__", board),
        ("__DATA__", data_json),
    ):
        out = out.replace(token, value)
    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, text: str, ctype: str = "text/html") -> None:
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.end_headers()
        self.wfile.write(text.encode())

    def do_GET(self):
        if self.path.startswith("/static/"):
            name = self.path.rsplit("/", 1)[-1]
            target = Path(__file__).resolve().parents[2] / "tui" / "static" / name
            if target.is_file() and target.parent == target.parent:
                self._send(target.read_text(), "application/javascript")
            else:
                self.send_response(404)
                self.end_headers()
            return
        if self.path.startswith("/api/candles?"):
            from urllib.parse import parse_qs, urlparse

            params = parse_qs(urlparse(self.path).query)
            try:
                payload = candles_payload(
                    params.get("symbol", [""])[0],
                    days=int(params.get("days", ["120"])[0]))
                self._send(json.dumps(payload, ensure_ascii=False),
                           "application/json")
            except Exception as exc:
                self._send(json.dumps({"error": str(exc)}),
                           "application/json")
            return
        with LOCK:
            payload = (json.dumps(CARD, ensure_ascii=False, default=str)
                       if self.path == "/api/card" else render(CARD))
        self._send(payload, "application/json" if self.path == "/api/card"
                   else "text/html")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        if self.path == "/api/holdings":
            from astock_signals import load_holdings, save_holdings

            state_root = (Config().state_root
                          or str(Path.home() / ".local/share/stammtisch"))
            symbol = str(body.get("symbol") or "").strip().upper()
            holdings = load_holdings(state_root)
            if body.get("action") == "add" and symbol and symbol not in holdings:
                holdings.append(symbol)
            elif body.get("action") == "remove" and symbol in holdings:
                holdings.remove(symbol)
            save_holdings(state_root, holdings)
            self._send(json.dumps({"status": f"holdings -> {holdings}"},
                                  ensure_ascii=False), "application/json")
        elif self.path == "/api/refresh":
            REFRESH_NOW.set()
            REFRESH_NOW.wait()
            with LOCK:
                payload = json.dumps(CARD, ensure_ascii=False, default=str)
            self._send(json.dumps({"status": "refreshed",
                                   "generated_at": CARD.get("generated_at")},
                                  ensure_ascii=False), "application/json")
        else:
            self._send("{}", "application/json")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=refresh_loop, daemon=True).start()
    time.sleep(1)
    print(f"A-share signal card: http://127.0.0.1:{PORT}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
