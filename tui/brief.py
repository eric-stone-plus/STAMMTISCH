"""Read-only daily-report rendering and legacy report discovery.

The primary D flow loads an explicit report artifact emitted by the daily
intake product. ``load_daily`` remains a compatibility reader for historical
report trees; it never scrapes and is not the D entry point.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


DEFAULT_REPORTS_ROOT = (
    Path.home() / ".local" / "share" / "stammtisch" / "daily-data" / "legacy-reports"
)
MARKET_KEYS = ("ashare", "hk", "us", "jp", "kr", "sg", "crypto")


def resolve_reports_root(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("STAMMTISCH_REPORTS") or os.environ.get("GALAHAD_REPORTS_ROOT")
    if env:
        return Path(env).expanduser()
    return DEFAULT_REPORTS_ROOT


def list_dates(reports_root: str | Path | None = None) -> list[str]:
    root = resolve_reports_root(reports_root)
    if not root.is_dir():
        return []
    out = []
    for p in root.iterdir():
        if p.is_dir() and len(p.name) == 8 and p.name.isdigit():
            if _json_product(p) is not None:
                out.append(p.name)
    return sorted(out)


def _json_product(date_dir: Path) -> Path | None:
    d = date_dir.name
    refined = date_dir / "output" / f"fin-daily-{d}.refined.json"
    filtered = date_dir / "output" / f"fin-daily-{d}.filtered.json"
    if refined.is_file():
        return refined
    if filtered.is_file():
        return filtered
    return None


BRIEF_MARKET_CAP = 10  # display cap; the report JSON keeps the full set
DISPLAY_MARKETS = ("ashare", "hk", "us")  # operator scope: A/H/US only
TITLE_CAP = 60
SUMMARY_CAP = 120

# Observed page-chrome denylist (IE upgrade pages, app tutorials, client
# download prompts).  Extend only with newly observed junk, keep it tight.
_JUNK_RE = re.compile(
    r"IE版本|浏览器版本|升至较高版本|download-ie|App操作教學|APP操作教学|"
    r"客户端下载|下载客户端|下载.*APP|APP.*下载",
    re.IGNORECASE,
)


def is_displayable_item(item: Any) -> bool:
    """Deterministic chrome/junk filter for the display layer."""
    if not isinstance(item, dict):
        return False
    title = str(item.get("title") or "")
    url = str(item.get("url") or "")
    if not title.strip():
        return False
    return _JUNK_RE.search(title) is None and _JUNK_RE.search(url) is None


def clean_text(text: Any, cap: int) -> str:
    """Collapse embedded newlines/whitespace and cap the display length."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) <= cap:
        return cleaned
    return cleaned[: cap - 1].rstrip() + "…"


def curate_items(items: list[Any], cap: int = BRIEF_MARKET_CAP) -> list[Any]:
    """Source-diverse deterministic pick for the display layer.

    Round-robin across sources (first-seen order) so one prolific feed
    cannot crowd out the rest; within a source the original order stands.
    """
    if len(items) <= cap:
        return list(items)
    lanes: dict[str, list[Any]] = {}
    for item in items:
        source = ""
        if isinstance(item, dict):
            source = str(item.get("source") or "")
            if not source:
                srcs = item.get("sources")
                if isinstance(srcs, list) and srcs:
                    source = str(srcs[0])
        lanes.setdefault(source, []).append(item)
    picked: list[Any] = []
    depth = 0
    while len(picked) < cap:
        progressed = False
        for lane in lanes.values():
            if depth < len(lane):
                picked.append(lane[depth])
                progressed = True
                if len(picked) >= cap:
                    break
        if not progressed:
            break
        depth += 1
    return picked


def _canonical_records_sibling(json_path: Path, day: str) -> list[dict[str, Any]] | None:
    """Full canonical records beside an intake-native report JSON, if any.

    The report layer is a curated view; the sibling canonical dataset is
    what sentiment should score.  A date mismatch fails closed.
    """
    candidate = json_path.parent / "canonical-dataset.json"
    try:
        doc = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    sibling_day = "".join(ch for ch in str(doc.get("date") or "") if ch.isdigit())
    if sibling_day != day:
        return None
    records = doc.get("records")
    if not isinstance(records, list) or not records:
        return None
    return [record for record in records if isinstance(record, dict)]


def load_daily(date: str | None = None, reports_root: str | Path | None = None) -> dict[str, Any]:
    """Return {ok, date, brief, markets, notes, json_path, html_path, model}."""
    root = resolve_reports_root(reports_root)
    if date:
        day = "".join(ch for ch in str(date) if ch.isdigit())
        if len(day) != 8:
            return {"ok": False, "error": f"bad date '{date}'", "date": date}
        date_dir = root / day
    else:
        dates = list_dates(root)
        if not dates:
            return {"ok": False, "error": f"no daily JSON under {root}", "date": None}
        day = dates[-1]
        date_dir = root / day

    jpath = _json_product(date_dir)
    if jpath is None:
        return {"ok": False, "error": f"no refined/filtered JSON in {date_dir}", "date": day}

    html_path = date_dir / "output" / f"fin-daily-{day}.html"
    return load_daily_path(jpath, html_path=html_path, expected_date=day)


def load_daily_path(
    json_path: str | Path,
    *,
    html_path: str | Path | None = None,
    expected_date: str | None = None,
) -> dict[str, Any]:
    """Load one explicit report JSON produced downstream of intake.

    The caller supplies the exact artifact selected by the verified intake
    envelope, so discovery can never make HTML or a stale report the source of
    truth. Source strings and report prose are passed through unchanged.
    """
    jpath = Path(json_path).expanduser()
    try:
        data = json.loads(jpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc), "date": expected_date}

    if not isinstance(data, dict):
        return {"ok": False, "error": "JSON root is not an object", "date": expected_date}

    raw_day = data.get("date") or expected_date
    day = "".join(ch for ch in str(raw_day or "") if ch.isdigit())
    if len(day) != 8:
        return {"ok": False, "error": "report JSON has no valid YYYYMMDD date", "date": raw_day}
    if expected_date and day != expected_date:
        return {
            "ok": False,
            "error": f"report date {day} does not match intake date {expected_date}",
            "date": day,
        }

    brief = data.get("brief") if isinstance(data.get("brief"), list) else []
    markets_in = data.get("markets") if isinstance(data.get("markets"), dict) else {}
    markets = {key: list(markets_in.get(key) or []) for key in MARKET_KEYS}
    notes = data.get("notes") if isinstance(data.get("notes"), list) else []
    resolved_html = Path(html_path).expanduser() if html_path else None
    tape = data.get("tape") if isinstance(data.get("tape"), dict) else None
    if tape is None:
        from .tape import build_tape
        tape = build_tape(
            brief, markets, notes, records=_canonical_records_sibling(jpath, day)
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
        "html_path": str(resolved_html) if resolved_html and resolved_html.is_file() else "",
    }


def format_brief(doc: dict[str, Any]) -> str:
    """Plain-text rendering for the TUI (not chart HTML)."""
    if not doc.get("ok"):
        return f"  brief error: {doc.get('error', '?')}\n"
    day = doc.get("date") or "?"
    pretty = f"{day[:4]}-{day[4:6]}-{day[6:]}" if len(day) == 8 else day
    lines = [f"  Fin-daily {pretty}  model={doc.get('model') or '?'}", ""]
    lines.append("  --- Brief ---")
    for i, b in enumerate(doc.get("brief") or [], 1):
        if not isinstance(b, dict):
            continue
        text = str(b.get("text") or "").strip()
        if not text:
            continue
        srcs = ", ".join(str(s) for s in (b.get("sources") or []) if s)
        lines.append(f"  {i}. {text}")
        if srcs:
            lines.append(f"     [{srcs}]")
    labels = {
        "ashare": "A-share",
        "hk": "HK",
        "us": "US",
    }
    for key in DISPLAY_MARKETS:
        items = [
            it
            for it in (doc.get("markets", {}).get(key) or [])
            if is_displayable_item(it)
        ]
        if not items:
            continue
        shown = curate_items(items)
        lines.append("")
        lines.append(f"  --- {labels[key]} ({len(items)}) ---")
        for it in shown:
            title = clean_text(it.get("title"), TITLE_CAP)
            url = str(it.get("url") or "").strip()
            summary = clean_text(it.get("summary"), SUMMARY_CAP)
            lines.append(f"  * {title}")
            if url:
                lines.append(f"    {url}")
            if summary and summary != title:
                lines.append(f"    {summary}")
        hidden = len(items) - len(shown)
        if hidden > 0:
            lines.append(f"  … +{hidden} more in the full dataset")
    notes = [str(n).strip() for n in (doc.get("notes") or []) if str(n).strip()]
    if notes:
        lines.append("")
        lines.append("  --- Notes ---")
        for n in notes:
            lines.append(f"  - {n}")
    return "\n".join(lines) + "\n"


try:
    from textual.app import ComposeResult
    from textual.binding import Binding
    from textual.containers import ScrollableContainer
    from textual.screen import Screen
    from textual.widgets import Footer, Static

    class BriefScreen(Screen):
        """Read-only fin-daily surface — not the K-line chart."""

        BINDINGS = [Binding("escape", "back", "Back")]
        CSS = """
        BriefScreen { layout: vertical; }
        #brief-body { height: 1fr; border: solid #505050; background: #000000; padding: 1 2; }
        """

        def __init__(self, doc: dict[str, Any], **kwargs: Any):
            super().__init__(**kwargs)
            self.doc = doc

        def compose(self) -> ComposeResult:
            day = self.doc.get("date") or "?"
            yield Static(
                f"  Fin-daily {day}  (read-only)  |  [Esc] Back",
                classes="header-bar",
            )
            yield ScrollableContainer(Static(format_brief(self.doc), id="brief-text"), id="brief-body")
            yield Footer()

        def action_back(self) -> None:
            self.app.pop_screen()

    class SentimentScreen(Screen):
        """Market tape + optional per-name overlay. Not the chart."""

        BINDINGS = [Binding("escape", "back", "Back")]
        CSS = """
        SentimentScreen { layout: vertical; }
        #sent-body { height: 1fr; border: solid #505050; background: #000000; padding: 1 2; }
        """

        def __init__(self, doc: dict[str, Any], symbol: str = "", **kwargs: Any):
            super().__init__(**kwargs)
            self.doc = doc
            self.symbol = symbol

        def compose(self) -> ComposeResult:
            from .tape import desk_sentiment
            day = self.doc.get("date") or "?"
            title = f"  SENTIMENT {day}"
            if self.symbol:
                title += f"  {self.symbol}"
            else:
                title += "  ALL MARKETS"
            title += "  (READ-ONLY)  |  [Esc] Back"
            yield Static(title, classes="header-bar")
            body = desk_sentiment(self.doc, self.symbol or None)
            if not body.strip():
                body = "  No daily report or sentiment tape is available.\n"
            yield ScrollableContainer(Static(body, id="sent-text"), id="sent-body")
            yield Footer()

        def action_back(self) -> None:
            self.app.pop_screen()
except ImportError:  # stdlib-only consumers still get load_daily
    BriefScreen = None  # type: ignore[misc, assignment]
    SentimentScreen = None  # type: ignore[misc, assignment]
