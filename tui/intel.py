"""First-hand intelligence for the SECURITY/FUTURES boards.

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
SZSE_SOURCE = "SZSE 公告 API"
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
            return json.loads(cache.read_text(encoding="utf-8"))
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
                why.append(f"陡back({slope:+.1f}%)")
            elif slope > 3:
                score -= 1
                why.append(f"contango({slope:+.1f}%)")
            else:
                why.append(f"曲线平({slope:+.1f}%)")
    if len(recent) >= 21 and recent[-1].get("close") and recent[-21].get("close"):
        momentum = (recent[-1]["close"] / recent[-21]["close"] - 1) * 100
        if momentum > 3:
            score += 0.5
            why.append(f"20D动量{momentum:+.1f}%")
        elif momentum < -3:
            score -= 0.5
            why.append(f"20D弱{momentum:+.1f}%")
    if score >= 1:
        reco = "多配"
    elif score <= -1:
        reco = "回避"
    elif score > 0:
        reco = "关注"
    else:
        reco = "中性"
    return reco, " ".join(why) + " · 来源: SGX结算/导出日线"
