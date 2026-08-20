"""Daily position decision runner — scan → verify → adjust, persisted.

One invocation per day (systemd timer, or ``--force`` to redo today):

- every zone of the configured ``security_symbols`` universe is scanned
  with the workstation's default strategy (per-symbol fetch retry, a
  transient provider hiccup costs one retry, not the zone);
- the latest crawled daily report contributes a per-market headline
  digest, and Kronos forecasts (when ``kronos_cmd`` is configured) cover
  each zone's top scan candidates — the decision sees quant evidence,
  yesterday's positions, the news tape, and a forward curve;
- GALAHAD adjusts yesterday's persisted decision into today's: prose
  rationale plus one fenced JSON positions block;
- the result persists under ``<state_root>/decisions/`` (``latest.json``
  plus a dated history copy). A failed round never leaves the state
  empty: the affected zone carries its previous decision forward with
  the error recorded, and a total failure exits non-zero without
  touching ``latest.json``.

Host-agnostic by construction: universe, model, forecast adapter, and
paths all come from the workstation config.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .engine import QuantEngine
from .screens.domains import security_zone

DECISION_VERSION = 1
KRONOS_TIMEOUT = 150
HEADLINES_PER_MARKET = 8


def _scan_zone(engine: QuantEngine, symbols: list[str]) -> list[dict[str, Any]]:
    """Backtest every symbol (2y default window); one retry per symbol."""
    start = (date.today() - timedelta(days=730)).isoformat()
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        result = None
        for attempt in (1, 2):
            result = engine.run_backtest(symbol, start=start)
            if result.get("ok"):
                break
            if attempt == 1:
                time.sleep(2.0)  # transient provider hiccups (delisted-style
                # errors are usually rate shapes): one retry, then it's real
        if not result or not result.get("ok"):
            rows.append({"symbol": symbol,
                         "error": str(result.get("error"))[:80] if result else "no result"})
            continue
        s = result["summary"]
        rows.append({
            "symbol": symbol,
            "tr": round(s.total_return * 100, 1),
            "cagr": round(s.cagr * 100, 1),
            "sharpe": round(s.sharpe, 2),
            "maxdd": round(s.max_drawdown * 100, 1),
            "win": round(s.win_rate * 100),
            "trades": s.trades,
        })
    ok = [r for r in rows if "error" not in r]
    ok.sort(key=lambda r: r["tr"], reverse=True)
    return ok + [r for r in rows if "error" in r]


def _table(rows: list[dict[str, Any]]) -> str:
    lines = ["symbol | TR% | CAGR% | sharpe | maxdd% | win% | trades"]
    for r in rows:
        if "error" in r:
            lines.append(f"{r['symbol']} | error: {r['error']}")
        else:
            lines.append(
                f"{r['symbol']} | {r['tr']:+.1f} | {r['cagr']:+.1f} | "
                f"{r['sharpe']:+.2f} | {r['maxdd']:.1f} | {r['win']} | {r['trades']}"
            )
    return "\n".join(lines)


def _kronos(config: Any, symbol: str) -> str | None:
    """One Kronos forecast summary line, or None when unavailable."""
    cmd = str(config.get("kronos_cmd") or "").strip()
    if not cmd:
        return None
    horizon = str(int(config.get("kronos_horizon") or 20))
    try:
        argv = shlex.split(cmd) + [symbol, "--horizon", horizon, "--json"]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=KRONOS_TIMEOUT)
        payload = json.loads(done.stdout)
        forecast = payload.get("forecast") or []
        if not forecast:
            return None
        last = float(forecast[-1])
        return (f"{symbol}: Kronos {payload.get('model', '?')} horizon "
                f"{len(forecast)}d -> last forecast {last:.2f}")
    except Exception:
        return None


def _headlines(config: Any, market: str) -> list[str]:
    """Top headlines of the latest crawled daily report for one market."""
    root = Path(str(config.get("workspace_root") or "")).expanduser()
    runs = root / "daily-data" / "runs"
    if not runs.is_dir():
        return []
    latest: tuple[str, dict[str, Any]] | None = None
    for path in sorted(runs.glob("*/fin-daily-*.json")):
        try:
            latest = (str(path), json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    if latest is None:
        return []
    items = ((latest[1].get("markets") or {}).get(market)) or []
    out = []
    for item in items[:HEADLINES_PER_MARKET]:
        title = str(item.get("title") or "").strip()
        if title:
            out.append(f"- [{item.get('source', '?')}] {title[:90]}")
    return out


def _parse_positions(text: str) -> dict[str, Any] | None:
    """Take the LAST fenced json block and require zone+positions keys."""
    blocks = []
    marker = "```"
    parts = text.split(marker)
    for index, part in enumerate(parts):
        stripped = part.lstrip()
        if index % 2 == 1 and stripped[:4].lower() == "json":
            blocks.append(part[stripped[:4].__len__():])
    for block in reversed(blocks):
        try:
            payload = json.loads(block.strip())
        except Exception:
            continue
        if isinstance(payload, dict) and "positions" in payload:
            return payload
    return None


def _prior_context(latest: dict[str, Any] | None, zone: str) -> str:
    zone_data = ((latest or {}).get("zones") or {}).get(zone)
    if not isinstance(zone_data, dict):
        return "(no prior decision on record)"
    positions = zone_data.get("positions") or []
    if not positions:
        return "(prior decision had no positions)"
    compact = ", ".join(
        f"{p.get('symbol')} {p.get('weight_pct')}% {p.get('action', '')}".strip()
        for p in positions if isinstance(p, dict)
    )
    return compact or "(prior decision unreadable)"


def run(force: bool = False) -> int:
    config = Config()
    engine = QuantEngine(data_dir=config.data_dir)

    if not config.ai_api_key:
        print("decide: AI API key is not configured", file=sys.stderr)
        return 2

    state_root = str(config.state_root or "").strip()
    base = Path(state_root).expanduser() if state_root else (
        Path.home() / ".local/share/stammtisch")
    decisions_root = base / "decisions"
    decisions_root.mkdir(parents=True, exist_ok=True)
    history_dir = decisions_root / "history"
    history_dir.mkdir(exist_ok=True)
    latest_path = decisions_root / "latest.json"

    latest: dict[str, Any] | None = None
    if latest_path.is_file():
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
        except Exception:
            latest = None

    today = date.today().isoformat()
    if latest and latest.get("date") == today and not force:
        print(f"decide: {today} decision already on record (use --force to redo)")
        return 0

    # ── universe by zone ───────────────────────────────────────────
    universe: dict[str, list[str]] = {}
    for symbol in config.security_symbols:
        universe.setdefault(security_zone(symbol), []).append(symbol)
    if not universe:
        print("decide: security_symbols is empty", file=sys.stderr)
        return 2

    from .ai_driver import AIDriver
    from .tools import default_tools
    ai = AIDriver(api_key=config.ai_api_key, base_url=config.ai_base_url,
                  model=config.ai_model, tools=default_tools(engine))

    market_by_zone = {"A-SHARE": "ashare", "HK": "hk", "US": "us"}
    outcomes: dict[str, Any] = {}
    failures = 0

    for zone, symbols in universe.items():
        print(f"[{zone}] scanning {len(symbols)} symbols…", flush=True)
        rows = _scan_zone(engine, symbols)
        ok_rows = [r for r in rows if "error" not in r]
        if not ok_rows:
            outcomes[zone] = {
                "error": "scan produced no data",
                "carried_from": (latest or {}).get("date"),
                "positions": (((latest or {}).get("zones") or {}).get(zone) or {}).get("positions"),
            }
            failures += 1
            continue

        top = [r["symbol"] for r in ok_rows[:3]]
        kronos_lines = [line for line in (_kronos(config, s) for s in top) if line]
        headlines = _headlines(config, market_by_zone.get(zone, ""))

        context_parts = [
            f"Today strategy scan — zone {zone}, dual_ma 20/50, cost low, 2y window, "
            f"sorted by TR:\n{_table(ok_rows)}",
            f"Yesterday's decision: {_prior_context(latest, zone)}",
        ]
        if headlines:
            context_parts.append("Today's crawled headlines (news tape):\n" + "\n".join(headlines))
        if kronos_lines:
            context_parts.append(
                "Kronos forecasts (auxiliary ONLY — the frozen model's curves "
                "are shape-collapsed across symbols, corr ≈ +0.96; treat as a "
                "weak prior, never as a standalone reason to cut a position):\n"
                + "\n".join(kronos_lines))

        prompt = (
            "你是每日配置决策人。基于扫描表、昨日决策、今日新闻与预测曲线,"
            "把昨日的持仓调整为今日决策:权重变化给理由,新增/剔除给证据,"
            "价格与回测数字一律以工具核实为准(优先用 scan_backtests 批量核实)。"
            "先给简短论述,然后必须以一个 ```json 代码块结尾,结构:"
            '{"zone":"…","stance":"proceed|cautious|defensive",'
            '"positions":[{"symbol":"…","weight_pct":0,"action":"hold|buy|add|trim|cut|watch","note":"…"}],'
            '"exclusions":[{"symbol":"…","reason":"…"}],'
            '"triggers":[{"watch":"…","action":"…"}]}. '
            "positions 必须覆盖你愿意持有的每一只(含 weight_pct=0 的 watch)。"
        )
        response = ai.chat(prompt, context="\n\n".join(context_parts))
        if response.error or not (response.content or "").strip():
            carried = (((latest or {}).get("zones") or {}).get(zone) or {})
            outcomes[zone] = {
                "error": str(response.error or "empty response")[:160],
                "carried_from": (latest or {}).get("date"),
                "positions": carried.get("positions"),
                "stance": carried.get("stance"),
            }
            failures += 1
            print(f"[{zone}] model round failed: {outcomes[zone]['error']}", file=sys.stderr)
            continue

        payload = _parse_positions(response.content)
        outcomes[zone] = {
            "stance": (payload or {}).get("stance"),
            "positions": (payload or {}).get("positions"),
            "exclusions": (payload or {}).get("exclusions"),
            "triggers": (payload or {}).get("triggers"),
            "structured": payload is not None,
            "rationale": response.content,
            "scan_rows": len(ok_rows),
        }
        structured = "structured" if payload else "PROSE-ONLY (unparsed)"
        print(f"[{zone}] decision recorded ({structured}, {len(ok_rows)} scanned)")

    if failures == len(universe):
        print("decide: every zone failed; previous latest.json left untouched",
              file=sys.stderr)
        return 1

    record = {
        "decision_version": DECISION_VERSION,
        "date": today,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": config.ai_model,
        "zones": outcomes,
    }
    tmp = latest_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(latest_path)
    (history_dir / f"{today}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"decide: {today} written to {latest_path}")
    return 0 if failures == 0 else 3


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in args
    return run(force=force)


if __name__ == "__main__":
    sys.exit(main())
