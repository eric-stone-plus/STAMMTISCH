"""HK daily bars + realtime quotes via Tencent endpoints.

Replaces the unstable yahoo path for .HK symbols: one kline endpoint
(ifzq.gtimg.cn fqkline, dividend-adjusted) caches into the standard
quantkit parquet layout, so the board, the screener and decide all read
it through the existing cache glob. Direct connection — the endpoints
are CN-side.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

KLINE_ENDPOINT = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
KLINE_SOURCE = "Tencent fqkline (qfq)"

_FIELDS_OK = {"code": 0}


def fetch_hk_daily(symbol: str, days: int = 800) -> pd.DataFrame | None:
    """Dividend-adjusted daily bars for one .HK symbol, newest last."""
    code = symbol.strip().upper().removesuffix(".HK").zfill(5)
    try:
        resp = requests.get(
            KLINE_ENDPOINT,
            params={"param": f"hk{code},day,,,{days},qfq"},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("code") not in (0, None):
        return None
    data = (payload.get("data") or {}).get(f"hk{code}") or {}
    rows = data.get("day") or data.get("qfqday") or []
    bars = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            bars.append({
                "date": row[0],
                "open": float(row[1]),
                "close": float(row[2]),
                "high": float(row[3]),
                "low": float(row[4]),
                "volume": float(row[5]),
            })
        except (ValueError, TypeError):
            continue
    if not bars:
        return None
    df = pd.DataFrame(bars).set_index("date")
    df.index = pd.to_datetime(df.index)
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def cache_path_for(data_dir: Path | str, symbol: str) -> Path:
    code = symbol.strip().upper().removesuffix(".HK").zfill(5)
    return Path(data_dir) / "cache" / f"tencent_auto_{code}.HK_1d.parquet"


def load_hk_cached(data_dir: Path | str, symbol: str,
                   max_age_days: int = 1) -> pd.DataFrame | None:
    """Fresh-enough cache first; else fetch and persist."""
    path = cache_path_for(data_dir, symbol)
    df: pd.DataFrame | None = None
    if path.is_file():
        try:
            df = pd.read_parquet(path)
        except Exception:
            df = None
    fresh = False
    if df is not None and len(df):
        last = pd.to_datetime(df.index[-1])
        # During CN hours a same-day bar is expected; overnight the last
        # session is yesterday — allow one calendar day of slack.
        fresh = (datetime.now() - last.to_pydatetime()) <= timedelta(
            days=max_age_days + 1)
    if fresh:
        return df
    fetched = fetch_hk_daily(symbol)
    if fetched is not None and len(fetched):
        path.parent.mkdir(parents=True, exist_ok=True)
        fetched.to_parquet(path)
        return fetched
    return df  # stale beats nothing
