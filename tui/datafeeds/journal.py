"""Quote snapshot journal — an append-only time & sales tape.

Every live poll of a board can append its snapshots to
``<state_root>/intel/quotes/YYYY-MM-DD.jsonl`` (one JSON object per
line: timestamp, symbol, last/prev_close/volume, and the provider
stamp). This is observability telemetry, not evidence: appends are
best-effort and a failed write never breaks a board, but the tape it
leaves behind is replayable for intraday review.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def snapshots_dir(state_root: str | Path) -> Path:
    return Path(state_root) / "intel" / "quotes"


def day_file(state_root: str | Path, day: str | None = None) -> Path:
    return snapshots_dir(state_root) / f"{day or datetime.now().strftime('%Y-%m-%d')}.jsonl"


def append(state_root: str | Path, quotes: dict[str, dict[str, Any]]) -> int:
    """Append one snapshot per quoted symbol to today's file.

    Returns the number of rows written; never raises — a failed write
    is a lost telemetry row, not a board error.
    """
    rows = []
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    for symbol, quote in quotes.items():
        last = quote.get("last")
        if last is None:
            continue
        rows.append(json.dumps({
            "ts": stamp,
            "symbol": symbol,
            "last": float(last),
            "prev_close": quote.get("prev_close"),
            "volume": quote.get("volume"),
            "source": quote.get("source") or "",
        }, ensure_ascii=False))
    if not rows:
        return 0
    try:
        path = day_file(state_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(rows) + "\n")
    except OSError:
        return 0
    return len(rows)


def read_tape(state_root: str | Path, symbol: str, *, days: int = 1,
              limit: int = 1000) -> list[dict[str, Any]]:
    """Replay the journal for one symbol, oldest first, at most ``limit`` rows."""
    wanted = symbol.strip().upper()
    out: list[dict[str, Any]] = []
    directory = snapshots_dir(state_root)
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.jsonl"))[-days:]:
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if str(row.get("symbol") or "").upper() == wanted:
                        out.append(row)
        except OSError:
            continue
    return out[-limit:]
