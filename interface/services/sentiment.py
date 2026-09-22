"""SentimentService — the daily-report tape, out of the screen.

Copy + adapt (no ``tui.`` import) of the old sentiment data side:

- the report load contract is copied from ``tui/brief.py:144-220``
  (``load_daily`` / ``load_daily_path``): explicit artifacts selected
  by the verified intake envelope (refined preferred over filtered),
  the in-doc date validated against the expected day (fail-closed),
  report prose passed through UNCHANGED, and the tape built from the
  sibling canonical dataset when the report carries none;
- the tape scorer/formatter is copied from ``tui/tape.py:14-422``
  (keyword kinds, weights, hype cap, stance bands, the English-chrome
  ``format_tape``, ``desk_sentiment``). The classification vocabulary
  includes Chinese terms — that is DATA (the scored newswire's own
  language), preserved verbatim per the RETIREMENT §5 fixture rule;
- the history index is ADAPTED from ``tui/history.py``: the old
  SQLite ``HistoryStore`` (incremental, mtime-gated) is old-tree
  machinery; this copy scans the two artifact shapes directly
  (``runs/*/fin-daily-<day>.json`` intake artifacts and legacy
  ``YYYYMMDD/output/fin-daily-<day>.{refined,filtered}.json`` trees),
  fail-closed per file, one entry per date with intake outranking
  legacy — the same ranking rule as the old ``list_reports``.

Adaptations: every path is an explicit argument (path injectables);
tests never read the real workspace — they pass ``tmp_path`` roots.
The default reports root keeps the old resolution order (explicit >
``STAMMTISCH_REPORTS`` / ``GALAHAD_REPORTS_ROOT`` > conventional home
path), evaluated at CALL time. The old per-symbol overlay
(``symbol_tape`` + the alias/needle machinery) is deliberately NOT
copied — it needs the old tree's offline symbol resolver and belongs
to the future domain screens, not this tape.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_REPORTS_ROOT",
    "MARKET_KEYS",
    "STANCES",
    "HistoryEntry",
    "build_tape",
    "classify",
    "desk_sentiment",
    "format_tape",
    "history_entries",
    "list_dates",
    "load_daily",
    "load_daily_path",
    "resolve_reports_root",
    "score_item",
    "tone",
]

# ── the tape scorer (tui/tape.py:14-59) ─────────────────────────────────

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
    """hard | flow | tape | soft | hype | rumor (tui/tape.py:80-96)."""
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
    """Raw bull/bear in [-1, 1] before kind weights (tui/tape.py:99-108)."""
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
    """Rows from the full canonical dataset (the uncurated newswire)."""
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
        text = (summary if summary and summary.startswith(title)
                else f"{title} {summary}")
        text = text.strip()
        if not text:
            continue
        sources = record.get("source_labels")
        if not isinstance(sources, list) or not sources:
            sources = [record.get("source_label") or record.get("source")
                       or ""]
        rows.append((market, text, [str(s) for s in sources if s]))
    return rows


def build_tape(
    brief: Any,
    markets: Any,
    notes: Any = None,
    *,
    records: Any = None,
) -> dict[str, Any]:
    """Stance object shown on the tape (tui/tape.py:166-314, copied).

    Hype share at or above ``_HYPE_CAP`` forces stance=watch even if the
    raw score looks constructive.  When ``records`` (the full canonical
    dataset) is supplied, scoring runs over every captured record instead
    of the curated report layer, and the tape gains kind, source and
    driver breakdowns.
    """
    rows = (_collect_records(records) if records is not None
            else _collect(brief, markets))
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
            source_bucket = by_source.setdefault(srcs[0], {"score": 0.0,
                                                           "n": 0})
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
    for bucket in by_market.values():
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
            f"Headline heat is elevated (hype/rumor "
            f"{hype_share * 100:.0f}%) — do not chase"
        )
    elif hype_n:
        caveats.append(f"Down-weighted {hype_n} hype/rumor item(s)")
    seen_caveats = set(caveats)
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


# ── the tape formatter (tui/tape.py:317-422) ────────────────────────────

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
        score_s = f"{float(score):+.2f}"
    except (TypeError, ValueError):
        score_s = "?"
    try:
        hype_s = f"{float(hype) * 100:.0f}%"
    except (TypeError, ValueError):
        hype_s = "?"
    lines = [
        "  --- MARKET SENTIMENT ---",
        (f"  STANCE={stance}  SCORE={score_s}  HYPE={hype_s}  "
         f"ITEMS={tape.get('n', 0)}"),
    ]
    bits = []
    for key in MARKET_KEYS:
        b = (tape.get("by_market") or {}).get(key) or {}
        if not b.get("n"):
            continue
        bits.append("{} {:+.2f} (HYPE {}/{})".format(
            _MARKET_LABEL[key], float(b.get("score") or 0),
            b.get("hype") or 0, b.get("n") or 0,
        ))
    if bits:
        lines.append("  " + "  |  ".join(bits))
    kinds = tape.get("kinds")
    if isinstance(kinds, dict) and kinds:
        order = ("hard", "flow", "tape", "soft", "hype", "rumor")
        detail = "  ".join(
            f"{_KIND_LABEL.get(key, key.upper())}={kinds[key]}"
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
                        str(drv.get("kind") or ""),
                        str(drv.get("kind") or "?").upper())
                    lines.append(f"  {sign} [{kind}] {drv['text']}")
    sources = tape.get("by_source")
    if isinstance(sources, list) and sources:
        cells = []
        for row in sources:
            if not isinstance(row, dict):
                continue
            try:
                cells.append("{} {:+.2f} ({})".format(
                    row.get("source") or "?",
                    float(row.get("score") or 0), row.get("n") or 0))
            except (TypeError, ValueError):
                continue
        if cells:
            lines.append("  SRC " + "  |  ".join(cells))
    for ev in tape.get("evidence") or []:
        # Items already shown as drivers are not repeated as evidence.
        if (isinstance(ev, dict) and ev.get("text")
                and str(ev["text"]) not in driver_texts):
            kind = _KIND_LABEL.get(
                str(ev.get("kind") or ""), str(ev.get("kind") or "?").upper())
            lines.append(f"  · [{kind}] {ev['text']}")
    for c in tape.get("caveats") or []:
        lines.append(f"  ! {c}")
    return "\n".join(lines) + "\n"


def desk_sentiment(doc: dict[str, Any]) -> str:
    """The workstation tape block (tui/tape.py:541-554, market-wide only)."""
    parts = []
    tape = doc.get("tape") if isinstance(doc, dict) else None
    if isinstance(tape, dict):
        parts.append(format_tape(tape).rstrip())
    return "\n\n".join(p for p in parts if p) + ("\n" if parts else "")


# ── the report load contract (tui/brief.py:17-220) ──────────────────────

DEFAULT_REPORTS_ROOT = (
    Path.home() / ".local" / "share" / "stammtisch" / "daily-data"
    / "legacy-reports"
)

_INTAKE_RE = re.compile(r"^fin-daily-(\d{8})\.json$")


def resolve_reports_root(explicit: str | Path | None = None) -> Path:
    """explicit > product env > conventional default (old brief.py:23-29)."""
    if explicit:
        return Path(explicit).expanduser()
    env = (os.environ.get("STAMMTISCH_REPORTS")
           or os.environ.get("GALAHAD_REPORTS_ROOT"))
    if env:
        return Path(env).expanduser()
    return DEFAULT_REPORTS_ROOT


def _json_product(date_dir: Path) -> Path | None:
    """Refined preferred over filtered (old brief.py:44-52)."""
    day = date_dir.name
    refined = date_dir / "output" / f"fin-daily-{day}.refined.json"
    filtered = date_dir / "output" / f"fin-daily-{day}.filtered.json"
    if refined.is_file():
        return refined
    if filtered.is_file():
        return filtered
    return None


def list_dates(reports_root: str | Path | None = None) -> list[str]:
    """Legacy ``YYYYMMDD`` dirs with a report product, sorted (old :32-41)."""
    root = resolve_reports_root(reports_root)
    if not root.is_dir():
        return []
    out = []
    for p in root.iterdir():
        if (p.is_dir() and len(p.name) == 8 and p.name.isdigit()
                and _json_product(p) is not None):
            out.append(p.name)
    return sorted(out)


def _canonical_records_sibling(json_path: Path,
                               day: str) -> list[dict[str, Any]] | None:
    """Full canonical records beside an intake-native report JSON (old
    brief.py:122-141): a date mismatch fails closed."""
    candidate = json_path.parent / "canonical-dataset.json"
    try:
        doc = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    sibling_day = "".join(ch for ch in str(doc.get("date") or "")
                          if ch.isdigit())
    if sibling_day != day:
        return None
    records = doc.get("records")
    if not isinstance(records, list) or not records:
        return None
    return [record for record in records if isinstance(record, dict)]


def load_daily_path(
    json_path: str | Path,
    *,
    html_path: str | Path | None = None,
    expected_date: str | None = None,
) -> dict[str, Any]:
    """Load one explicit report JSON (old brief.py:167-220, copied).

    The caller supplies the exact artifact, so discovery can never make
    HTML or a stale report the source of truth. Source strings and
    report prose pass through unchanged. Returns the doc dict:
    ``{ok, date, model, brief, markets, notes, tape, json_path,
    html_path}`` (``ok=False, error`` on any failure).
    """
    jpath = Path(json_path).expanduser()
    try:
        data = json.loads(jpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "date": expected_date}

    if not isinstance(data, dict):
        return {"ok": False, "error": "JSON root is not an object",
                "date": expected_date}

    raw_day = data.get("date") or expected_date
    day = "".join(ch for ch in str(raw_day or "") if ch.isdigit())
    if len(day) != 8:
        return {"ok": False, "error": "report JSON has no valid YYYYMMDD "
                                      "date", "date": raw_day}
    if expected_date and day != expected_date:
        return {
            "ok": False,
            "error": f"report date {day} does not match intake date "
                     f"{expected_date}",
            "date": day,
        }

    brief = data.get("brief") if isinstance(data.get("brief"), list) else []
    markets_in = (data.get("markets")
                  if isinstance(data.get("markets"), dict) else {})
    markets = {key: list(markets_in.get(key) or [])
               for key in MARKET_KEYS}
    notes = data.get("notes") if isinstance(data.get("notes"), list) else []
    resolved_html = Path(html_path).expanduser() if html_path else None
    tape = data.get("tape") if isinstance(data.get("tape"), dict) else None
    if tape is None:
        tape = build_tape(
            brief, markets, notes,
            records=_canonical_records_sibling(jpath, day),
        )
    return {
        "ok": True,
        "date": day,
        "model": data.get("model", ""),
        "brief": brief,
        "markets": markets,
        "notes": notes,
        "tape": tape,
        "json_path": str(jpath),
        "html_path": (str(resolved_html)
                      if resolved_html and resolved_html.is_file() else ""),
    }


def load_daily(date: str | None = None,
               reports_root: str | Path | None = None) -> dict[str, Any]:
    """Latest (or one dated) legacy report (old brief.py:144-164, copied)."""
    root = resolve_reports_root(reports_root)
    if date:
        day = "".join(ch for ch in str(date) if ch.isdigit())
        if len(day) != 8:
            return {"ok": False, "error": f"bad date '{date}'", "date": date}
        date_dir = root / day
    else:
        dates = list_dates(root)
        if not dates:
            return {"ok": False, "error": f"no daily JSON under {root}",
                    "date": None}
        day = dates[-1]
        date_dir = root / day

    jpath = _json_product(date_dir)
    if jpath is None:
        return {"ok": False,
                "error": f"no refined/filtered JSON in {date_dir}",
                "date": day}

    html_path = date_dir / "output" / f"fin-daily-{day}.html"
    return load_daily_path(jpath, html_path=html_path, expected_date=day)


# ── the history index (adapted from tui/history.py) ─────────────────────


@dataclass(frozen=True)
class HistoryEntry:
    """One indexed daily-report artifact (the old ReportEntry surface).

    The screen reads ``report_date`` / ``json_path`` / ``html_path`` /
    ``origin`` only.
    """

    report_date: str
    origin: str  # intake | legacy
    json_path: str
    html_path: str


def _history_date(doc: dict[str, Any]) -> str:
    raw = "".join(ch for ch in str(doc.get("date") or "") if ch.isdigit())
    return raw if len(raw) == 8 else ""


def _scan_intake(workspace_root: Path) -> list[HistoryEntry]:
    """``runs/<run-id>/fin-daily-<day>.json`` (old history.py:163-166).

    Fail-closed per file (old _parse): the artifact name is the intake
    contract's date anchor — a mismatch with the in-doc date means the
    file is not a trustworthy report and is skipped.
    """
    entries: list[HistoryEntry] = []
    if not workspace_root.is_dir():
        return entries
    for path in sorted(workspace_root.glob("runs/*/fin-daily-*.json")):
        match = _INTAKE_RE.match(path.name)
        if match is None:
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        day = _history_date(doc)
        # The artifact name is the intake contract's date anchor; a
        # missing or mismatching in-doc date skips the file (old _parse).
        if not day or day != match.group(1):
            continue
        html = path.with_suffix(".html")
        entries.append(HistoryEntry(
            report_date=day, origin="intake", json_path=str(path),
            html_path=str(html) if html.is_file() else "",
        ))
    return entries


def _scan_legacy(legacy_root: Path) -> list[HistoryEntry]:
    """Legacy ``YYYYMMDD/output`` report JSON, refined preferred (old
    history.py:168-181)."""
    entries: list[HistoryEntry] = []
    if not legacy_root.is_dir():
        return entries
    for date_dir in sorted(legacy_root.glob("[0-9]" * 8)):
        if not date_dir.is_dir():
            continue
        output = date_dir / "output"
        refined = output / f"fin-daily-{date_dir.name}.refined.json"
        filtered = output / f"fin-daily-{date_dir.name}.filtered.json"
        candidate = refined if refined.is_file() else filtered
        if not candidate.is_file():
            continue
        html = output / f"fin-daily-{date_dir.name}.html"
        entries.append(HistoryEntry(
            report_date=date_dir.name, origin="legacy",
            json_path=str(candidate),
            html_path=str(html) if html.is_file() else "",
        ))
    return entries


def history_entries(
    reports_root: str | Path | None = None,
    workspace_root: str | Path | None = None,
) -> tuple[HistoryEntry, ...]:
    """One entry per report date, NEWEST first, intake outranking legacy.

    Adapted from the old HistoryStore.list_reports ranking (old
    history.py:282-297): the SQLite store is old-tree machinery — this
    scan reads the same two artifact shapes directly, fail-closed per
    file. ``reports_root`` feeds the legacy scan (and, when no explicit
    workspace root is given, its ``runs/`` subtree feeds the intake scan
    too, so one root can host both shapes).
    """
    root = resolve_reports_root(reports_root)
    entries = _scan_legacy(root)
    workspace = (Path(workspace_root).expanduser()
                 if workspace_root is not None else root)
    entries.extend(_scan_intake(workspace))
    best: dict[str, HistoryEntry] = {}
    for entry in entries:
        current = best.get(entry.report_date)
        if current is None or (entry.origin == "intake"
                               and current.origin != "intake"):
            best[entry.report_date] = entry
    return tuple(sorted(best.values(),
                        key=lambda e: e.report_date, reverse=True))
