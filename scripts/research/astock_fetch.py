"""A-share daily panel fetcher — CN-direct, runs under quant-python.

Primary source: Tencent fqkline (ifzq.gtimg.cn) — the same CN-side
endpoint family the workstation's live quotes ride, proven reachable
from this host with no proxy. Fallback: akshare's eastmoney hist when
it is not throttling. Both are dividend-adjusted (qfq) daily closes.

Proxies are refused (session trust_env off, ambient vars stripped):
CN-direct discipline — these endpoints must never ride an egress.
Writes one JSON panel ({symbol: [[date, close], ...]}) to the state
root; per-symbol failures are skipped and reported, never fatal.

Usage: quant-python astock_fetch.py --out <path> [--days 400] [--workers 10]
       (symbols arrive on stdin as a JSON list, or via --symbols)
"""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Lock

for _var in ("http_proxy", "https_proxy", "all_proxy",
             "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(_var, None)

TENCENT_KLINE = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"


def tencent_code(symbol: str) -> str | None:
    text = str(symbol).strip().upper()
    if text.endswith((".SS", ".SH")):
        return "sh" + text.split(".")[0]
    if text.endswith(".SZ"):
        return "sz" + text.split(".")[0]
    if text.endswith(".BJ"):
        return "bj" + text.split(".")[0]
    if text.isdigit():
        prefix = {"6": "sh", "9": "sh", "5": "sh"}.get(text[0], "sz")
        return prefix + text
    return None


def fetch_tencent(symbol: str, days: int) -> list[list]:
    import requests

    code = tencent_code(symbol)
    if not code:
        raise ValueError(f"unmappable {symbol}")
    session = requests.Session()
    session.trust_env = False  # CN-direct: never ride ambient proxies
    resp = session.get(TENCENT_KLINE,
                       params={"param": f"{code},day,,,{days},qfq"},
                       headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    resp.raise_for_status()
    data = resp.json().get("data", {}).get(code) or {}
    rows = data.get("qfqday") or data.get("day") or []
    return [[str(r[0]), float(r[2])] for r in rows if len(r) >= 3]


def fetch_akshare(symbol: str, start: str, end: str) -> list[list]:
    import akshare as ak

    code = str(symbol).strip().upper().split(".")[0]
    df = ak.stock_zh_a_hist(symbol=code, period="daily",
                            start_date=start, end_date=end, adjust="qfq")
    return [[str(d.date()), float(c)] for d, c in zip(df["日期"], df["收盘"])]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--days", type=int, default=400)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--symbols", nargs="*")
    args = parser.parse_args()

    symbols = args.symbols or json.loads(
        Path("/dev/stdin").read_text() or "[]")
    if not symbols:
        print("no symbols", flush=True)
        return 2

    start = (date.today() - timedelta(days=args.days)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    panel: dict[str, list] = {}
    errors: dict[str, str] = {}
    lock = Lock()

    def fetch(symbol: str) -> None:
        name = str(symbol).strip().upper()
        rows: list[list] = []
        error = ""
        try:
            rows = fetch_tencent(name, args.days)
        except Exception as exc:  # noqa: BLE001 - try the fallback source
            error = f"tencent:{type(exc).__name__}"
            try:
                rows = fetch_akshare(name, start, end)
                error = ""  # fallback served
            except Exception as exc2:  # noqa: BLE001 - per-symbol degrade
                error += f"+akshare:{type(exc2).__name__}"
        if rows and not error:
            with lock:
                panel[name] = rows
        elif error:
            with lock:
                errors[name] = error

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(fetch, symbols))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "asof": max((rows[-1][0] for rows in panel.values()), default=""),
        "panel": panel, "errors": errors}), encoding="utf-8")
    tmp.replace(args.out)
    print(f"panel: {len(panel)} symbols, {len(errors)} errors -> {args.out}",
          flush=True)
    return 0 if panel else 1


if __name__ == "__main__":
    raise SystemExit(main())
