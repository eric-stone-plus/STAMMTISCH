"""Daily-report history index backed by SQLite.

The D screen lands on the most recent indexed report and the H picker
browses every indexed date.  Two origins feed one store:

- ``intake``: ``runs/<run-id>/fin-daily-*.json`` artifacts under the
  daily-data workspace (the current canonical product);
- ``legacy``: ``YYYYMMDD/output/fin-daily-<day>.{refined,filtered}.json``
  trees kept for historical reproduction.

Indexing is incremental (mtime-gated) and fail-closed per file: one
unreadable report is skipped and never aborts a scan.  Only report JSON is
indexed; report HTML is discovered as a sibling, never parsed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    json_path TEXT PRIMARY KEY,
    report_date TEXT NOT NULL,
    origin TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    markets TEXT NOT NULL DEFAULT '{}',
    items_total INTEGER NOT NULL DEFAULT 0,
    sources_succeeded INTEGER NOT NULL DEFAULT 0,
    sources_expected INTEGER NOT NULL DEFAULT 0,
    html_path TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    source_mtime REAL NOT NULL,
    ingested_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_date ON reports (report_date);
"""

INTAKE = "intake"
LEGACY = "legacy"

_INTAKE_RE = re.compile(r"^fin-daily-(\d{8})\.json$")
_LEGACY_RE = re.compile(r"^fin-daily-(\d{8})\.(refined|filtered)\.json$")


# Operator scope: only these markets surface in the picker rows.  The store
# itself keeps every market's counts.
DISPLAY_MARKETS = ("ashare", "hk", "us")


@dataclass(frozen=True)
class ReportEntry:
    """One indexed daily-report artifact."""

    report_date: str
    origin: str
    run_id: str
    model: str
    markets: dict[str, int]
    items_total: int
    sources_succeeded: int
    sources_expected: int
    json_path: str
    html_path: str
    sha256: str
    ingested_at: float

    @property
    def pretty_date(self) -> str:
        day = self.report_date
        return f"{day[:4]}-{day[4:6]}-{day[6:]}" if len(day) == 8 else day

    def row_text(self) -> str:
        """One-line picker summary, session-list style."""
        coverage = "  ".join(
            f"{market} {self.markets[market]}"
            for market in DISPLAY_MARKETS
            if self.markets.get(market)
        )
        shown_items = sum(self.markets.get(market, 0) for market in DISPLAY_MARKETS)
        parts = [self.pretty_date, self.origin.ljust(6), coverage or "-"]
        parts.append(f"{shown_items} items")
        if self.sources_expected:
            parts.append(f"src {self.sources_succeeded}/{self.sources_expected}")
        return "  ".join(parts)


def _date_from(doc: dict[str, Any]) -> str:
    raw = "".join(ch for ch in str(doc.get("date") or "") if ch.isdigit())
    return raw if len(raw) == 8 else ""


def _market_counts(doc: dict[str, Any]) -> dict[str, int]:
    raw = doc.get("market_counts")
    if isinstance(raw, dict):
        counts = {
            key: value
            for key, value in raw.items()
            if isinstance(key, str) and key and isinstance(value, int)
        }
        if counts:
            return counts
    markets = doc.get("markets")
    out: dict[str, int] = {}
    if isinstance(markets, dict):
        for key, value in markets.items():
            if isinstance(key, str) and key and isinstance(value, list):
                out[key] = len(value)
    return out


def _intake_stats(doc: dict[str, Any]) -> tuple[int, int]:
    raw = doc.get("intake")
    if not isinstance(raw, dict):
        return 0, 0
    succeeded = raw.get("succeeded")
    expected = raw.get("expected")
    return (
        succeeded if isinstance(succeeded, int) else 0,
        expected if isinstance(expected, int) else 0,
    )


class HistoryStore:
    """Append-friendly SQLite index over daily-report JSON artifacts."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        return conn

    # -- indexing -----------------------------------------------------------

    def index_all(
        self,
        workspace_root: str | Path | None,
        legacy_root: str | Path | None = None,
    ) -> dict[str, int]:
        stats = {"scanned": 0, "indexed": 0, "unchanged": 0, "skipped": 0}
        if workspace_root:
            self._accumulate(stats, self.index_workspace(workspace_root))
        if legacy_root:
            self._accumulate(stats, self.index_legacy_tree(legacy_root))
        return stats

    @staticmethod
    def _accumulate(stats: dict[str, int], other: dict[str, int]) -> None:
        for key in stats:
            stats[key] += other.get(key, 0)

    def index_workspace(self, workspace_root: str | Path) -> dict[str, int]:
        """Index intake-native reports: runs/<run-id>/fin-daily-<day>.json."""
        root = Path(workspace_root).expanduser()
        return self._scan(sorted(root.glob("runs/*/fin-daily-*.json")), INTAKE)

    def index_legacy_tree(self, legacy_root: str | Path) -> dict[str, int]:
        """Index legacy YYYYMMDD/output report JSON, refined preferred."""
        root = Path(legacy_root).expanduser()
        paths: list[Path] = []
        for date_dir in sorted(root.glob("[0-9]" * 8)):
            if not date_dir.is_dir():
                continue
            output = date_dir / "output"
            refined = output / f"fin-daily-{date_dir.name}.refined.json"
            filtered = output / f"fin-daily-{date_dir.name}.filtered.json"
            candidate = refined if refined.is_file() else filtered
            if candidate.is_file():
                paths.append(candidate)
        return self._scan(paths, LEGACY)

    def _scan(self, paths: list[Path], origin: str) -> dict[str, int]:
        stats = {"scanned": 0, "indexed": 0, "unchanged": 0, "skipped": 0}
        with self._connect() as conn:
            for path in paths:
                stats["scanned"] += 1
                outcome = self._index_file(conn, path, origin)
                stats[outcome] += 1
        return stats

    def _index_file(self, conn: sqlite3.Connection, path: Path, origin: str) -> str:
        """Returns 'indexed', 'unchanged', or 'skipped' (fail-closed)."""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return "skipped"
        row = conn.execute(
            "SELECT source_mtime FROM reports WHERE json_path = ?", (str(path),)
        ).fetchone()
        if row is not None and float(row["source_mtime"]) == mtime:
            return "unchanged"
        entry = self._parse(path, origin)
        if entry is None:
            return "skipped"
        conn.execute(
            """
            INSERT OR REPLACE INTO reports (
                json_path, report_date, origin, run_id, model, markets,
                items_total, sources_succeeded, sources_expected,
                html_path, sha256, source_mtime, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.json_path,
                entry.report_date,
                entry.origin,
                entry.run_id,
                entry.model,
                json.dumps(entry.markets, sort_keys=True),
                entry.items_total,
                entry.sources_succeeded,
                entry.sources_expected,
                entry.html_path,
                entry.sha256,
                mtime,
                time.time(),
            ),
        )
        return "indexed"

    def _parse(self, path: Path, origin: str) -> ReportEntry | None:
        try:
            raw = path.read_bytes()
            doc = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(doc, dict):
            return None
        day = _date_from(doc)
        if origin == INTAKE:
            if not day:
                return None
            match = _INTAKE_RE.match(path.name)
            if match is None or match.group(1) != day:
                # The artifact name is the intake contract's date anchor; a
                # mismatch means the file is not a trustworthy report.
                return None
            run_id = str(doc.get("run_id") or path.parent.name)
            html = path.with_suffix(".html")
        else:
            match = _LEGACY_RE.match(path.name)
            if match is None:
                return None
            file_day = match.group(1)
            if day and day != file_day:
                return None
            # Early legacy JSON carried no in-doc date; the directory and
            # filename date are the legacy contract's anchor.
            day = day or file_day
            run_id = ""
            html = path.parent / f"fin-daily-{day}.html"
        markets = _market_counts(doc)
        succeeded, expected = _intake_stats(doc)
        return ReportEntry(
            report_date=day,
            origin=origin,
            run_id=run_id,
            model=str(doc.get("model") or ""),
            markets=markets,
            items_total=sum(markets.values()),
            sources_succeeded=succeeded,
            sources_expected=expected,
            json_path=str(path),
            html_path=str(html) if html.is_file() else "",
            sha256=hashlib.sha256(raw).hexdigest(),
            ingested_at=time.time(),
        )

    # -- queries ------------------------------------------------------------

    def list_reports(self) -> list[ReportEntry]:
        """One row per report date, newest first.

        An intake artifact always outranks a legacy import of the same
        date; within one origin the latest run wins (run ids are
        timestamp-prefixed, so lexicographic order is chronological).
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM reports").fetchall()
        best: dict[str, ReportEntry] = {}
        for row in rows:
            entry = self._to_entry(row)
            current = best.get(entry.report_date)
            if current is None or self._rank(entry) > self._rank(current):
                best[entry.report_date] = entry
        return sorted(best.values(), key=lambda entry: entry.report_date, reverse=True)

    @staticmethod
    def _rank(entry: ReportEntry) -> tuple[int, str, float]:
        return (1 if entry.origin == INTAKE else 0, entry.run_id, entry.ingested_at)

    def latest(self) -> ReportEntry | None:
        entries = self.list_reports()
        return entries[0] if entries else None

    def get(self, report_date: str) -> ReportEntry | None:
        day = "".join(ch for ch in str(report_date) if ch.isdigit())
        for entry in self.list_reports():
            if entry.report_date == day:
                return entry
        return None

    @staticmethod
    def _to_entry(row: sqlite3.Row) -> ReportEntry:
        try:
            markets = json.loads(row["markets"])
        except (TypeError, json.JSONDecodeError):
            markets = {}
        if not isinstance(markets, dict):
            markets = {}
        return ReportEntry(
            report_date=str(row["report_date"]),
            origin=str(row["origin"]),
            run_id=str(row["run_id"]),
            model=str(row["model"]),
            markets={str(key): int(value) for key, value in markets.items()},
            items_total=int(row["items_total"]),
            sources_succeeded=int(row["sources_succeeded"]),
            sources_expected=int(row["sources_expected"]),
            json_path=str(row["json_path"]),
            html_path=str(row["html_path"]),
            sha256=str(row["sha256"]),
            ingested_at=float(row["ingested_at"]),
        )
