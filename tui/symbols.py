"""Multi-market ticker resolve + search (CN / HK / JP / KR / US).

Bare codes become Yahoo-style tickers. Ambiguous numbers (e.g. 005930)
return more than one hit so the chart dropdown can ask; ``normalize_symbol``
keeps the first hit so existing A-share callers stay stable.
"""

from __future__ import annotations

from typing import Any


# Well-known names so "tencent" / "apple" resolve without a network.
# Keep this list short; the dropdown also accepts raw tickers.
_NAMES: list[tuple[str, str, str]] = [
    ("AAPL", "US", "Apple"),
    ("MSFT", "US", "Microsoft"),
    ("GOOGL", "US", "Alphabet"),
    ("AMZN", "US", "Amazon"),
    ("NVDA", "US", "Nvidia"),
    ("TSLA", "US", "Tesla"),
    ("META", "US", "Meta"),
    ("BABA", "US", "Alibaba"),
    ("0700.HK", "HK", "Tencent"),
    ("9988.HK", "HK", "Alibaba HK"),
    ("3690.HK", "HK", "Meituan"),
    ("1810.HK", "HK", "Xiaomi"),
    ("0941.HK", "HK", "China Mobile"),
    ("9992.HK", "HK", "Pop Mart"),
    ("005930.KS", "KR", "Samsung Electronics"),
    ("000660.KS", "KR", "SK Hynix"),
    ("005380.KS", "KR", "Hyundai Motor"),
    ("035420.KS", "KR", "Naver"),
    ("7203.T", "JP", "Toyota"),
    ("6758.T", "JP", "Sony"),
    ("9984.T", "JP", "SoftBank"),
    ("7974.T", "JP", "Nintendo"),
    ("6861.T", "JP", "Keyence"),
    ("600098.SS", "CN", "Guangzhou Development"),
    ("600519.SS", "CN", "Kweichow Moutai"),
    ("601318.SS", "CN", "Ping An"),
    ("600036.SS", "CN", "China Merchants Bank"),
    ("000001.SZ", "CN", "Ping An Bank"),
    ("000858.SZ", "CN", "Wuliangye"),
    ("300750.SZ", "CN", "CATL"),
]

_SUFFIX_MARKET = {
    ".SS": "CN", ".SH": "CN", ".SZ": "CN", ".BJ": "CN",
    ".HK": "HK",
    ".T": "JP", ".TYO": "JP",
    ".KS": "KR", ".KQ": "KR",
}

_SUFFIX_MIC = {
    ".SS": "XSHG",
    ".SH": "XSHG",
    ".SZ": "XSHE",
    ".BJ": "XBEI",
    ".HK": "XHKG",
    ".T": "XTKS",
    ".TYO": "XTKS",
    ".KS": "XKRX",
    ".KQ": "XKRX",
}


_NAME_BY_SYM = {sym: (mkt, name) for sym, mkt, name in _NAMES}


def _hit(symbol: str, market: str, name: str = "") -> dict[str, str]:
    known = _NAME_BY_SYM.get(symbol)
    if known:
        market, name = known
    hit = {"symbol": symbol, "market": market, "name": name}
    upper = symbol.upper()
    for suffix, mic in sorted(_SUFFIX_MIC.items(), key=lambda item: -len(item[0])):
        if upper.endswith(suffix):
            hit["mic"] = mic
            break
    # A bare US ticker may trade on more than one venue. Search results do
    # not invent an exchange identity merely to make validated lookup pass.
    return hit


def hk_yahoo_symbol(digits: str) -> str:
    """Official HK codes are 5-digit (09992). Yahoo wants 9992.HK / 0700.HK."""
    d = "".join(ch for ch in digits if ch.isdigit())
    core = d.lstrip("0") or "0"
    if len(core) <= 4:
        return core.zfill(4) + ".HK"
    return core + ".HK"


def _cn_suffix(code: str) -> str | None:
    if len(code) != 6 or not code.isdigit():
        return None
    if code[0] in ("6", "9"):
        return f"{code}.SS"
    if code[0] in ("0", "2", "3"):
        return f"{code}.SZ"
    if code[0] in ("4", "8"):
        return f"{code}.BJ"
    return None


def _explicit_market(sym: str) -> str | None:
    u = sym.upper()
    for suf, mkt in _SUFFIX_MARKET.items():
        if u.endswith(suf):
            return mkt
    if u.endswith("-USD") or "/" in u or u.endswith("USDT"):
        return "US"
    return None


def resolve_query(raw: str, market: str | None = None) -> list[dict[str, str]]:
    """Return ranked search hits for a typed query. Never hits the network."""
    q = (raw or "").strip()
    if not q:
        return []
    filt = (market or "").strip().upper()
    if filt in ("", "ALL", "*"):
        filt = ""

    hits: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(symbol: str, mkt: str, name: str = "") -> None:
        if filt and mkt != filt:
            return
        if symbol in seen:
            return
        seen.add(symbol)
        hits.append(_hit(symbol, mkt, name))

    u = q.upper().replace(" ", "")
    if u.endswith(".SH"):
        u = u[:-3] + ".SS"

    if u.endswith(".HK"):
        add(hk_yahoo_symbol(u[:-3]), "HK")
        return hits

    explicit = _explicit_market(u)
    if explicit:
        add(u, explicit)
        return hits

    if u.isdigit():
        if len(u) == 6:
            cn = _cn_suffix(u)
            if cn:
                add(cn, "CN")
            # 6-digit 0xxxxx is also a common KOSPI shape (005930).
            if u[0] == "0":
                add(f"{u}.KS", "KR")
        elif len(u) == 5 or (len(u) <= 4 and u[0] == "0"):
            # 09992 / 0700 / 700 → Yahoo HK (9992.HK / 0700.HK)
            add(hk_yahoo_symbol(u), "HK")
        elif len(u) == 4:
            hk = hk_yahoo_symbol(u)
            jp = f"{u}.T"
            if hk in _NAME_BY_SYM and jp not in _NAME_BY_SYM:
                add(hk, "HK")
                add(jp, "JP")
            else:
                add(jp, "JP")
                add(hk, "HK")
        else:
            add(hk_yahoo_symbol(u), "HK")
        return hits

    # Letters: US ticker, plus name index.
    if u.isalnum() and not u.isdigit():
        add(u, "US")

    ql = q.lower()
    for sym, mkt, name in _NAMES:
        if ql in name.lower() or ql in sym.lower():
            add(sym, mkt, name)

    return hits


def normalize_symbol(symbol: str) -> str:
    """Primary ticker used by fetch/engine. First resolve hit, else passthrough."""
    q = (symbol or "").strip()
    if not q:
        return q
    hits = resolve_query(q)
    if hits:
        return hits[0]["symbol"]
    return q.upper()


def search_payload(q: str, market: str | None = None, limit: int = 12) -> dict[str, Any]:
    hits = resolve_query(q, market)[: max(1, min(int(limit), 30))]
    return {"ok": True, "q": q, "hits": hits}
