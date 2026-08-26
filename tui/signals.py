"""First-hand market signals for the SECURITY/FUTURES boards.

Every value carries its source; degraded pieces are omitted, never
faked.  Cheap and cache-friendly: announcements are fetched once per
day into the state root, sentiment is read from the fin-daily tape.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

SZSE_ENDPOINT = "http://www.szse.cn/api/report/ShowReport/data"
SZSE_SOURCE = "SZSE announcements API"
_ST_TOKEN_RE = re.compile(r"(?:^|[^A-Za-z])\*?ST(?:[^A-Za-z]|$)")
_SIGNALS = {
    "dividend": ["分红", "派息", "利润分配", "DIVIDEND"],
    "egm": ["股东大会", "EGM", "AGM"],
    "risk": ["退市", "暂停上市", "风险警示"],
    "merger": ["合并", "收购", "重组"],
    "buyback": ["回购"],
    "pledge": ["质押"],
}


def _intel_root(state_root: str | None) -> Path:
    base = Path(state_root).expanduser() if state_root else (
        Path.home() / ".local/share/stammtisch")
    out = base / "intel"
    out.mkdir(parents=True, exist_ok=True)
    return out


def classify_title(title: str) -> list[str]:
    text = title.upper()
    found = []
    for category, keywords in _SIGNALS.items():
        if any(kw.upper() in text for kw in keywords):
            found.append(category)
    if _ST_TOKEN_RE.search(title.upper()):
        found.append("risk")
    return found


def load_announcements(state_root: str | None, days: int = 30,
                       max_pages: int = 12) -> list[dict[str, Any]]:
    """Recent SZSE announcements, cached per day under the state root."""
    cache = _intel_root(state_root) / f"szse-announcements-{date.today().isoformat()}.json"
    if cache.is_file():
        try:
            rows = json.loads(cache.read_text(encoding="utf-8"))
            for row in rows:
                row["source"] = SZSE_SOURCE  # normalize cached labels
            return rows
        except (ValueError, OSError):
            pass
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        try:
            resp = requests.get(
                SZSE_ENDPOINT,
                params={"SHOWTYPE": "JSON", "CATALOGID": "1845", "TABKEY": "tab1",
                        "PAGENO": str(page), "PAGESIZE": "50"},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException:
            break
        batch = data[0].get("data", []) if isinstance(data, list) and data else []
        if not batch:
            break
        stop = False
        for r in batch:
            if r.get("plrq", "") < cutoff:
                stop = True
                break
            rows.append({
                "symbol": r.get("zqdm", "") + ".SZ" if r.get("zqdm") else "",
                "date": r.get("plrq", ""),
                "title": r.get("ggbt", ""),
                "signals": classify_title(r.get("ggbt", "")),
                "source": SZSE_SOURCE,
            })
        if stop:
            break
        time.sleep(0.5)
    cache.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return rows


def signals_for(symbols: list[str], state_root: str | None) -> dict[str, list[dict]]:
    """Announcement signals keyed by board symbol (last 30d)."""
    wanted = {s.upper() for s in symbols}
    out: dict[str, list[dict]] = {}
    for row in load_announcements(state_root):
        symbol = (row.get("symbol") or "").upper()
        if symbol in wanted and row.get("signals"):
            out.setdefault(symbol, []).append(row)
    return out


def sentiment_stance(market: str, reports_root: Path | None) -> dict[str, Any] | None:
    """Today's tape stance for a market ('hk' / 'us' / ...), if present."""
    if reports_root is None:
        return None
    try:
        candidates = sorted(reports_root.glob("*.json"))[-3:]
    except OSError:
        return None
    for path in reversed(candidates):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for block in doc.get("markets", []) if isinstance(doc.get("markets"), list) else []:
            if block.get("market") == market:
                return {
                    "score": block.get("score"),
                    "stance": block.get("stance"),
                    "items": block.get("items") or block.get("count"),
                    "source": block.get("source") or "fin-daily tape",
                    "date": str(path.stem)[:10],
                }
    return None


def futures_reco(item: dict[str, Any]) -> tuple[str, str]:
    """Deterministic futures row recommendation from stable daily data."""
    curve = item.get("curve") or []
    recent = item.get("recent") or []
    why: list[str] = []
    score = 0.0
    if len(curve) >= 2:
        front, back = curve[0]["settle"], curve[-1]["settle"]
        if front and back:
            slope = (back / front - 1) * 100
            # steep backwardation (front >> back) = tight prompt market
            if slope < -3:
                score += 1
                why.append(f"steep backw {slope:+.1f}%")
            elif slope > 3:
                score -= 1
                why.append(f"contango {slope:+.1f}%")
            else:
                why.append(f"flat curve {slope:+.1f}%")
    if len(recent) >= 21 and recent[-1].get("close") and recent[-21].get("close"):
        momentum = (recent[-1]["close"] / recent[-21]["close"] - 1) * 100
        if momentum > 3:
            score += 0.5
            why.append(f"20D mom {momentum:+.1f}%")
        elif momentum < -3:
            score -= 0.5
            why.append(f"20D weak {momentum:+.1f}%")
    if score >= 1:
        reco = "LONG"
    elif score <= -1:
        reco = "AVOID"
    elif score > 0:
        reco = "WATCH"
    else:
        reco = "NEUTRAL"
    return reco, " ".join(why) + " · src: SGX settle / export bars"


# ── CCI (China Securities commodity futures indices) daily board ────────

CCI_DAILY_SOURCE = "CCI daily parquet (ccidx daily fetch)"
_CCI_CATEGORY = {
    "000001": "COMPOSITE", "000002": "COMPOSITE",
    "100001": "COMPOSITE",
    "000101": "INDUSTRIALS", "100101": "INDUSTRIALS",
    "000102": "AGRI-LIVESTOCK", "100102": "AGRI-LIVESTOCK",
    "000103": "PRECIOUS", "100103": "PRECIOUS",
    "000104": "ENERGY-CHEM", "100104": "ENERGY-CHEM",
    "000105": "NONFERROUS", "100105": "NONFERROUS",
    "000106": "FERROUS", "100106": "FERROUS",
    "000107": "AGRI-PRODUCTS", "100107": "AGRI-PRODUCTS",
    "000108": "INDUSTRIALS", "100108": "INDUSTRIALS",
    "630101": "BONDS", "630102": "BONDS", "630103": "BONDS",
    "900001": "COMPOSITE",
}


def cci_category(index_id: str) -> str:
    if index_id in _CCI_SUBCATEGORY:
        return _CCI_SUBCATEGORY[index_id]
    code = index_id.split(".")[0]
    return _CCI_CATEGORY.get(code, "COMPOSITE")


def cci_daily_board(data_dir: Path | None) -> list[dict[str, Any]]:
    """One row per CCI index from the cached daily parquets."""
    if data_dir is None:
        return []
    try:
        import pandas as pd
    except ImportError:
        return []
    rows: list[dict[str, Any]] = []
    base = Path(data_dir)
    paths = sorted(base.glob("cci_auto_*.parquet"))
    paths += sorted((base / "cache").glob("cci_auto_*.parquet"))
    for path in paths:
        stem = path.name.split("_")
        if len(stem) < 3:
            continue
        index_id = stem[2] + ".CCI" if not stem[2].endswith(".CCI") else stem[2]
        try:
            df = pd.read_parquet(path)
        except Exception:
            continue
        if "close" not in df.columns or len(df) < 25:
            continue
        closes = [float(v) for v in df["close"].dropna()]
        if not closes:
            continue
        last, prev = closes[-1], closes[-2] if len(closes) > 1 else None

        def _pct(days: int) -> float | None:
            if len(closes) > days and closes[-1 - days] > 0:
                return round((last / closes[-1 - days] - 1) * 100, 2)
            return None

        recent = [
            {
                "date": str(ts)[:10],
                **({"open": round(float(row["open"]), 4),
                    "high": round(float(row["high"]), 4),
                    "low": round(float(row["low"]), 4)}
                   if all(c in df.columns for c in ("open", "high", "low")) else {}),
                "close": round(float(row["close"]), 4),
                "volume": float(row.get("volume", 0) or 0),
            }
            for ts, row in df.tail(12).iterrows()
        ]
        rows.append({
            "key": f"cci:{index_id}", "source": "cci",
            "code": index_id,
            "category": cci_category(index_id),
            "name": cci_name(index_id),
            "unit": "pt",
            "last": last,
            "chg_pct": round((last / prev - 1) * 100, 2) if prev else None,
            "pct5": _pct(5), "pct20": _pct(20),
            "volume": float(df.get("volume", pd.Series([0] * len(df))).iloc[-1] or 0),
            "recent": recent,
            "curve": [],
            "intel_source": CCI_DAILY_SOURCE,
        })
    return rows


def cci_reco(row: dict[str, Any]) -> tuple[str, str]:
    """Deterministic momentum read for one CCI index row."""
    why: list[str] = []
    score = 0.0
    for label, value in (("5D", row.get("pct5")), ("20D", row.get("pct20"))):
        if value is None:
            continue
        if value > 2:
            score += 1 if label == "20D" else 0.5
            why.append(f"{label} {value:+.1f}%")
        elif value < -2:
            score -= 1 if label == "20D" else 0.5
            why.append(f"{label} {value:+.1f}%")
    reco = ("LONG" if score >= 1 else "AVOID" if score <= -1
            else "WATCH" if score > 0 else "NEUTRAL")
    return reco, (" ".join(why) + f" · {CCI_DAILY_SOURCE}").strip()


def szse_search_link(code: str) -> str:
    """Stable per-stock disclosure search link on szse.cn."""
    return f"https://www.szse.cn/application/search/index.html?keyword={code.split('.')[0]}"


_MD_LINK = re.compile(r"\[([^\]]{20,140})\]\((https://www\.fool\.com/[^)\s]{10,140})\)")


def us_headlines(reports_root: Path | None, limit: int = 4) -> list[dict[str, str]]:
    """Headline + link pairs from the latest fin-daily US (fool) dump."""
    if reports_root is None:
        return []
    try:
        days = sorted(p for p in reports_root.iterdir() if p.is_dir())[-2:]
    except OSError:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for day in reversed(days):
        md = day / f"fin-us-{day.name}" / "fool.md"
        if not md.is_file():
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in _MD_LINK.finditer(text):
            title = " ".join(match.group(1).replace("\n", " ").split())
            url = match.group(2)
            if url.rstrip("/") in seen or "mms/mark" in url:
                continue
            if title.startswith(("Arrow", "Accessibility", "Log In")):
                continue
            if not any(seg in url for seg in
                       ("/investing/", "/market-activity", "/quote/", "/news/", "/articles/")):
                continue
            seen.add(url.rstrip("/"))
            out.append({"title": title[:90], "url": url,
                        "source": "Motley Fool daily dump"})
            if len(out) >= limit:
                return out
    return out


# Official index names (data, from the ccidx catalog used by the
# fetch scripts) and refined sub-index categories.
CCI_NAMES = {
    "000001.CCI": "中证商品期货价格指数",
    "000101.CCI": "中证工业品期货价格指数",
    "000102.CCI": "中证农畜期货价格指数",
    "000103.CCI": "中证贵金属期货价格指数",
    "000104.CCI": "中证能化期货价格指数",
    "000105.CCI": "中证有色金属期货价格指数",
    "000106.CCI": "中证黑色期货价格指数",
    "000107.CCI": "中证农产品期货价格指数",
    "000108.CCI": "中证畜产品价格",
    "000109.CCI": "中证能源价格",
    "000110.CCI": "中证化工价格",
    "000111.CCI": "中证油脂油料价格",
    "000112.CCI": "中证谷物价格",
    "000113.CCI": "中证软商品价格",
    "000114.CCI": "中证油品价格",
    "000115.CCI": "中证烯烃价格",
    "000116.CCI": "中证芳烃价格",
    "000117.CCI": "中证聚酯价格",
    "000118.CCI": "中证橡胶价格",
    "000119.CCI": "中证黑色原料价格",
    "000120.CCI": "中证黑色成材价格",
    "100001.CCI": "中证商品期货指数",
    "100101.CCI": "中证工业品期货指数",
    "100102.CCI": "中证农畜期货指数",
    "100103.CCI": "中证贵金属期货指数",
    "100104.CCI": "中证能化期货指数",
    "100105.CCI": "中证有色金属期货指数",
    "100106.CCI": "中证黑色期货指数",
    "100107.CCI": "中证农产品期货指数",
    "606001.CCI": "中证监控中国商品期货指数",
    "606002.CCI": "中证监控中国农产品期货指数",
    "606009.CCI": "中证监控中国工业品期货指数",
    "606010.CCI": "监控能化",
    "606011.CCI": "监控钢铁",
    "630101.CCI": "中国国债期货收益指数10年期",
    "630102.CCI": "中国国债期货收益指数5年期",
    "630103.CCI": "中国国债期货收益指数2年期",
    "900001.CCI": "中证中金公司商品期货综合指数"
}

_CCI_SUBCATEGORY = {
    "000108.CCI": "AGRI-PRODUCTS",
    "000109.CCI": "ENERGY-CHEM",
    "000110.CCI": "ENERGY-CHEM",
    "000111.CCI": "AGRI-PRODUCTS",
    "000112.CCI": "AGRI-PRODUCTS",
    "000113.CCI": "AGRI-PRODUCTS",
    "000114.CCI": "ENERGY-CHEM",
    "000115.CCI": "ENERGY-CHEM",
    "000116.CCI": "ENERGY-CHEM",
    "000117.CCI": "ENERGY-CHEM",
    "000118.CCI": "ENERGY-CHEM",
    "000119.CCI": "FERROUS",
    "000120.CCI": "FERROUS",
    "606001.CCI": "MONITOR",
    "606002.CCI": "MONITOR",
    "606009.CCI": "MONITOR",
    "606010.CCI": "MONITOR",
    "606011.CCI": "MONITOR",
    "900001.CCI": "COMPOSITE"
}


def cci_name(index_id: str) -> str:
    return CCI_NAMES.get(index_id, index_id)
