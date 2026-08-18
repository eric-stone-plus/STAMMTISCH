"""Tape-watch sentiment from a fin-daily product.

Headlines are classified before they are scored. Hype and rumor get a
thin weight so a promotion-heavy day cannot flip the stance to chase.
Source text remains untouched; deterministic UI chrome is English.
"""

from __future__ import annotations

import re
from typing import Any


MARKET_KEYS = ("ashare", "hk", "us")  # operator scope: A/H/US only
STANCES = ("defensive", "watch", "mixed", "constructive")

_HYPE = (
    "爆发", "涨停", "跌停", "概念", "必看", "暴涨", "狂飙", "起飞", "神话",
    "造富", "热炒", "跟风", "游资", "龙头战法", "宇宙", "全民",
    "soars", "skyrocket", "meme", "fomo", "mooning", "parabolic",
    "all-time high", "ath",
)
_RUMOR = (
    "据报", "传闻", "或将", "拟", "疑似", "未证实", "爆料", "知情人士",
    "rumor", "rumour", "reportedly", "sources say", "unconfirmed",
)
_HARD = (
    "央行", "货币政策", "财报", "净利润", "营收", "纯利", "ppi", "cpi",
    "非农", "逆回购", "guidelines", "earnings", "revenue", "guidance",
    "fed ", "federal reserve", "wholesale prices", "利率决议",
)
_FLOW = (
    "净买入", "净卖出", "南向", "北向", "资金流入", "资金流出",
    "southbound", "northbound", "etf flow",
)
_BULL = (
    "上涨", "超预期", "增长", "盈利", "反弹", "升", "放量",
    "beat", "upgrade", "outperform", "higher", "rise", "gain",
)
_BEAR = (
    "下跌", "不及预期", "下降", "亏损", "减持", "暴跌", "回落",
    "miss", "downgrade", "underperform", "fall", "drop", "slip",
    "selloff", "sell-off",
)
_INDEX = (
    "上证", "深成指", "创业板", "恒指", "恒生", "标普", "纳斯达克",
    "s&p", "nasdaq", "dow", "hang seng",
)

_WEIGHT = {
    "hard": 1.0,
    "tape": 0.55,
    "flow": 0.50,
    "soft": 0.35,
    "hype": 0.12,
    "rumor": 0.08,
}

_HYPE_CAP = 0.35


def _blob(item: Any) -> str:
    if isinstance(item, dict):
        return " ".join(
            str(item.get(k) or "") for k in ("text", "title", "summary")
        )
    if isinstance(item, (tuple, list)) and item:
        return str(item[0] or "")
    return str(item or "")


def _sources(item: Any) -> list[str]:
    if isinstance(item, dict):
        return [str(s) for s in (item.get("sources") or []) if s]
    if isinstance(item, (tuple, list)) and len(item) > 2:
        return [str(item[2])]
    return []


def classify(text: str) -> str:
    """hard | flow | tape | soft | hype | rumor."""
    t = (text or "").strip()
    low = t.lower()
    if any(w in t or w in low for w in _RUMOR):
        return "rumor"
    if any(w in t or w in low for w in _HYPE):
        return "hype"
    if any(w in t or w in low for w in _HARD) or re.search(
        r"\d+(?:\.\d+)?\s*(?:%|亿元|亿港元|亿美元|bps)", t
    ):
        return "hard"
    if any(w in t or w in low for w in _FLOW):
        return "flow"
    if any(w in t or w in low for w in _INDEX):
        return "tape"
    return "soft"


def tone(text: str) -> float:
    """Raw bull/bear in [-1, 1] before kind weights."""
    t = (text or "")
    low = t.lower()
    pos = sum(1 for w in _BULL if w in t or w in low)
    neg = sum(1 for w in _BEAR if w in t or w in low)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def score_item(text: str) -> dict[str, Any]:
    kind = classify(text)
    raw = tone(text)
    weight = _WEIGHT[kind]
    return {
        "kind": kind,
        "tone": round(raw, 4),
        "weight": weight,
        "score": round(raw * weight, 4),
    }


def _collect(brief: Any, markets: Any) -> list[tuple[str, str, list[str]]]:
    rows: list[tuple[str, str, list[str]]] = []
    for b in brief or []:
        text = _blob(b)
        if text.strip():
            rows.append(("brief", text, _sources(b)))
    mk = markets if isinstance(markets, dict) else {}
    for key in MARKET_KEYS:
        for it in mk.get(key) or []:
            text = _blob(it)
            if text.strip():
                rows.append((key, text, _sources(it)))
    return rows


def _collect_records(records: Any) -> list[tuple[str, str, list[str]]]:
    """Rows from the full canonical dataset (the unscurated newswire)."""
    rows: list[tuple[str, str, list[str]]] = []
    if not isinstance(records, list):
        return rows
    for record in records:
        if not isinstance(record, dict):
            continue
        market = str(record.get("market") or "")
        if market not in MARKET_KEYS:
            # Out-of-scope captures (crypto and retired desks) never reach
            # the sentiment tape.
            continue
        title = str(record.get("title") or "")
        summary = str(record.get("summary") or "")
        # Raw captures often repeat the title at the head of the summary;
        # keep the merged blob free of that echo.
        text = summary if summary and summary.startswith(title) else f"{title} {summary}"
        text = text.strip()
        if not text:
            continue
        sources = record.get("source_labels")
        if not isinstance(sources, list) or not sources:
            sources = [record.get("source_label") or record.get("source") or ""]
        rows.append((market, text, [str(s) for s in sources if s]))
    return rows


def build_tape(
    brief: Any,
    markets: Any,
    notes: Any = None,
    *,
    records: Any = None,
) -> dict[str, Any]:
    """Stance object written into refined.json and shown in the TUI.

    Hype share at or above _HYPE_CAP forces stance=watch even if the
    raw score looks constructive.  When ``records`` (the full canonical
    dataset) is supplied, scoring runs over every captured record instead
    of the curated report layer, and the tape gains kind, source and
    driver breakdowns.
    """
    rows = _collect_records(records) if records is not None else _collect(brief, markets)
    scored: list[dict[str, Any]] = []
    by_market: dict[str, dict[str, Any]] = {}
    for key in MARKET_KEYS:
        by_market[key] = {"score": 0.0, "n": 0, "hype": 0, "hard": 0}
    kinds: dict[str, int] = {}
    by_source: dict[str, dict[str, Any]] = {}

    weighted = 0.0
    mass = 0.0
    hype_n = 0
    for market, text, srcs in rows:
        hit = score_item(text)
        hit["text"] = text[:160]
        hit["market"] = market
        hit["sources"] = srcs
        scored.append(hit)
        weighted += hit["score"]
        mass += hit["weight"]
        kinds[hit["kind"]] = kinds.get(hit["kind"], 0) + 1
        if srcs:
            source_bucket = by_source.setdefault(srcs[0], {"score": 0.0, "n": 0})
            source_bucket["score"] += hit["score"]
            source_bucket["n"] += 1
        if hit["kind"] in ("hype", "rumor"):
            hype_n += 1
        if market in by_market:
            bucket = by_market[market]
            bucket["n"] += 1
            bucket["score"] += hit["score"]
            if hit["kind"] in ("hype", "rumor"):
                bucket["hype"] += 1
            if hit["kind"] == "hard":
                bucket["hard"] += 1

    n = len(scored)
    hype_share = (hype_n / n) if n else 0.0
    score = (weighted / mass) if mass else 0.0
    for key, bucket in by_market.items():
        if bucket["n"]:
            bucket["score"] = round(bucket["score"] / bucket["n"], 4)
        bucket["score"] = round(float(bucket["score"]), 4)

    stance = "watch"
    if n >= 4 and hype_share < _HYPE_CAP:
        if score >= 0.28:
            stance = "constructive"
        elif score <= -0.28:
            stance = "defensive"
        elif abs(score) >= 0.12:
            stance = "mixed"

    caveats: list[str] = []
    if hype_share >= _HYPE_CAP:
        caveats.append(
            "Headline heat is elevated (hype/rumor %.0f%%) — do not chase"
            % (hype_share * 100)
        )
    elif hype_n:
        caveats.append("Down-weighted %d hype/rumor item(s)" % hype_n)
    seen_caveats = {c for c in caveats}
    for nte in notes or []:
        s = str(nte).strip()
        if s and s not in seen_caveats:
            caveats.append(s)
            seen_caveats.add(s)
        if len(caveats) >= 6:
            break

    evidence = sorted(
        (h for h in scored if h["kind"] in ("hard", "tape", "flow")),
        key=lambda h: abs(h["score"]),
        reverse=True,
    )[:4]
    evidence = [
        {"text": h["text"], "kind": h["kind"], "score": h["score"],
         "market": h["market"]}
        for h in evidence
    ]

    def _driver(hit: dict[str, Any]) -> dict[str, Any]:
        return {
            "text": hit["text"],
            "kind": hit["kind"],
            "score": hit["score"],
            "market": hit["market"],
            "source": hit["sources"][0] if hit["sources"] else "",
        }

    driver_pool = [h for h in scored if h["kind"] in ("hard", "flow")]
    drivers = {
        "bull": [
            _driver(h)
            for h in sorted(
                (h for h in driver_pool if h["score"] > 0.05),
                key=lambda h: h["score"],
                reverse=True,
            )[:3]
        ],
        "bear": [
            _driver(h)
            for h in sorted(
                (h for h in driver_pool if h["score"] < -0.05),
                key=lambda h: h["score"],
            )[:3]
        ],
    }
    sources_ranked = sorted(
        (
            {
                "source": name,
                "n": bucket["n"],
                "score": round(bucket["score"] / bucket["n"], 4),
                "total": round(bucket["score"], 4),
            }
            for name, bucket in by_source.items()
            if bucket["n"]
        ),
        key=lambda row: abs(row["total"]),
        reverse=True,
    )[:6]

    return {
        "stance": stance,
        "score": round(score, 4),
        "hype_share": round(hype_share, 4),
        "n": n,
        "by_market": by_market,
        "kinds": kinds,
        "by_source": sources_ranked,
        "drivers": drivers,
        "caveats": caveats,
        "evidence": evidence,
    }


_STANCE_LABEL = {
    "constructive": "CONSTRUCTIVE",
    "defensive": "DEFENSIVE",
    "mixed": "MIXED",
    "watch": "WATCH",
}
_KIND_LABEL = {
    "hard": "HARD",
    "tape": "TAPE",
    "flow": "FLOW",
    "soft": "SOFT",
    "hype": "HYPE",
    "rumor": "RUMOR",
}
_MARKET_LABEL = {
    "ashare": "A-SHARE",
    "hk": "HK",
    "us": "US",
    "jp": "JP",
    "kr": "KR",
    "sg": "SG",
    "crypto": "CRYPTO",
}


def format_tape(tape: dict[str, Any] | None) -> str:
    """Plain-text block with English chrome and untouched evidence text."""
    if not tape or not isinstance(tape, dict):
        return ""
    raw_stance = str(tape.get("stance") or "watch").lower()
    stance = _STANCE_LABEL.get(raw_stance, raw_stance.upper())
    score = tape.get("score")
    hype = tape.get("hype_share")
    try:
        score_s = "%+.2f" % float(score)
    except (TypeError, ValueError):
        score_s = "?"
    try:
        hype_s = "%.0f%%" % (float(hype) * 100)
    except (TypeError, ValueError):
        hype_s = "?"
    lines = [
        "  --- MARKET SENTIMENT ---",
        f"  STANCE={stance}  SCORE={score_s}  HYPE={hype_s}  ITEMS={tape.get('n', 0)}",
    ]
    bits = []
    for key in MARKET_KEYS:
        b = (tape.get("by_market") or {}).get(key) or {}
        if not b.get("n"):
            continue
        bits.append("%s %+.2f (HYPE %s/%s)" % (
            _MARKET_LABEL[key], float(b.get("score") or 0),
            b.get("hype") or 0, b.get("n") or 0,
        ))
    if bits:
        lines.append("  " + "  |  ".join(bits))
    kinds = tape.get("kinds")
    if isinstance(kinds, dict) and kinds:
        order = ("hard", "flow", "tape", "soft", "hype", "rumor")
        detail = "  ".join(
            "%s=%s" % (_KIND_LABEL.get(key, key.upper()), kinds[key])
            for key in order
            if kinds.get(key)
        )
        if detail:
            lines.append("  KINDS " + detail)
    drivers = tape.get("drivers")
    driver_texts: set[str] = set()
    if isinstance(drivers, dict):
        for sign, key in (("+", "bull"), ("-", "bear")):
            for drv in drivers.get(key) or []:
                if isinstance(drv, dict) and drv.get("text"):
                    driver_texts.add(str(drv["text"]))
                    kind = _KIND_LABEL.get(
                        str(drv.get("kind") or ""), str(drv.get("kind") or "?").upper()
                    )
                    lines.append("  %s [%s] %s" % (sign, kind, drv["text"]))
    sources = tape.get("by_source")
    if isinstance(sources, list) and sources:
        cells = []
        for row in sources:
            if not isinstance(row, dict):
                continue
            try:
                cells.append(
                    "%s %+.2f (%s)"
                    % (row.get("source") or "?", float(row.get("score") or 0), row.get("n") or 0)
                )
            except (TypeError, ValueError):
                continue
        if cells:
            lines.append("  SRC " + "  |  ".join(cells))
    for ev in tape.get("evidence") or []:
        # Items already shown as drivers are not repeated as evidence.
        if (
            isinstance(ev, dict)
            and ev.get("text")
            and str(ev["text"]) not in driver_texts
        ):
            kind = _KIND_LABEL.get(
                str(ev.get("kind") or ""), str(ev.get("kind") or "?").upper()
            )
            lines.append("  · [%s] %s" % (kind, ev["text"]))
    for c in tape.get("caveats") or []:
        lines.append("  ! %s" % c)
    return "\n".join(lines) + "\n"


# Entity link: industry desks score a *name*, not the whole newswire.
# Aliases stay short; resolve_query covers English names in symbols.py.
_ALIASES: dict[str, tuple[str, ...]] = {
    "0700.HK": ("tencent", "腾讯", "0700"),
    "9618.HK": ("jd.com", "京东", "9618"),
    "9988.HK": ("alibaba", "阿里", "9988"),
    "9992.HK": ("pop mart", "泡泡玛特", "9992"),
    "1810.HK": ("xiaomi", "小米", "1810"),
    "0981.HK": ("smic", "中芯国际", "中芯", "0981"),
    "600519.SS": ("moutai", "茅台", "600519"),
    "600098.SS": ("guangzhou development", "广州发展", "600098"),
    "300750.SZ": ("catl", "宁德时代", "300750"),
    "AAPL": ("apple", "苹果"),
    "NVDA": ("nvidia", "英伟达"),
    "TSLA": ("tesla", "特斯拉"),
    "BTC-USD": ("bitcoin", "比特币"),
}


def needles_for_symbol(raw: str) -> set[str]:
    from .symbols import normalize_symbol, resolve_query

    sym = normalize_symbol(raw or "")
    out = {raw.lower(), sym.lower()}
    core = "".join(ch for ch in sym.split(".")[0] if ch.isdigit() or ch.isalpha())
    if len(core) >= 3:
        out.add(core.lower())
        if core.isdigit():
            out.add(core.lstrip("0") or "0")
    try:
        for h in resolve_query(raw):
            out.add(str(h.get("symbol") or "").lower())
            name = str(h.get("name") or "").strip()
            if len(name) >= 3:
                out.add(name.lower())
    except Exception:
        pass
    for key, aliases in _ALIASES.items():
        blob = {key.lower(), *aliases}
        if raw.lower() in blob or sym.lower() in blob:
            out.update(blob)
    return {n for n in out if len(n) >= 2}


def mentions(text: str, needles: set[str]) -> bool:
    if not text or not needles:
        return False
    low = text.lower()
    for n in needles:
        if len(n) <= 3 and all(ch.isascii() for ch in n):
            if re.search(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(n), low):
                return True
        elif n in low or n in text:
            return True
    return False


def symbol_tape(symbol: str, brief: Any, markets: Any) -> dict[str, Any]:
    """Per-ticker read: only headlines that mention the name.

    Empty hits → stance=watch, n=0. Hype on that name still cannot
    become constructive.
    """
    from .symbols import normalize_symbol

    sym = normalize_symbol(symbol or "")
    needles = needles_for_symbol(symbol)
    matched: list[tuple[str, str, list[str]]] = []
    for market, text, srcs in _collect(brief, markets):
        if mentions(text, needles):
            matched.append((market, text, srcs))
    fake = {k: [] for k in MARKET_KEYS}
    hits = []
    for market, text, srcs in matched:
        key = market if market in MARKET_KEYS else "ashare"
        fake[key].append({"title": text, "sources": srcs})
        hit = score_item(text)
        hits.append({
            "text": text[:160],
            "kind": hit["kind"],
            "score": hit["score"],
            "market": key,
            "sources": srcs,
        })
    tape = build_tape([], fake, [])
    tape["symbol"] = sym
    tape["hits"] = hits
    if not hits:
        tape["stance"] = "watch"
        tape["caveats"] = ["No matching headline for this symbol today"]
    return tape


def format_symbol_tape(tape: dict[str, Any] | None) -> str:
    if not tape or not isinstance(tape, dict):
        return ""
    sym = tape.get("symbol") or "?"
    head = format_tape({k: tape.get(k) for k in (
        "stance", "score", "hype_share", "n", "by_market",
        "caveats", "evidence",
    )})
    lines = ["  --- SYMBOL %s ---" % sym]
    if head:
        # reuse chrome without the market-wide title
        for line in head.splitlines():
            if line.startswith("  --- MARKET SENTIMENT"):
                continue
            lines.append(line)
    for h in tape.get("hits") or []:
        if isinstance(h, dict) and h.get("text"):
            lines.append("  > [%s %+0.2f] %s" % (
                h.get("kind") or "?", float(h.get("score") or 0), h["text"],
            ))
    return "\n".join(lines) + "\n"


def desk_sentiment(doc: dict[str, Any], symbol: str | None = None) -> str:
    """Workstation panel: market tape, then optional name overlay."""
    parts = []
    tape = doc.get("tape") if isinstance(doc, dict) else None
    if isinstance(tape, dict):
        parts.append(format_tape(tape).rstrip())
    if symbol:
        st = symbol_tape(
            symbol,
            (doc or {}).get("brief"),
            (doc or {}).get("markets"),
        )
        parts.append(format_symbol_tape(st).rstrip())
    return "\n\n".join(p for p in parts if p) + ("\n" if parts else "")
