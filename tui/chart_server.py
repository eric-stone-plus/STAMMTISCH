"""Local chart server — the browser viewport for K-line charts.

The browser renders TradingView Lightweight Charts (vendored under
`tui/static/`); this server only feeds it data. Stdlib-only on
purpose: no new dependencies for the TUI.

Routes:

  GET /                      the chart page
  GET /chart/<symbol>        the chart page preloaded with a symbol
  GET /api/candles?symbol=...&start=...   OHLCV JSON (quantkit)
  GET /api/forecast?symbol=...[&horizon=N] forecast JSON (kronos_cmd)

Run directly (``python -m tui.chart_server [--port PORT]``) or let the
TUI start it on demand when a chart is opened.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .config import Config
from .engine import _missing_ohlcv, _normalize_symbol
from .symbols import search_payload
from .timeseries import TimeseriesDriver

STATIC_DIR = Path(__file__).parent / "static"
INDEX = Path(__file__).parent / "web_chart.html"
DEFAULT_PORT = 0
VALIDATED_REFERENCE_SCHEMA = "stammtisch.validated-bars-reference.v1"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MIC_RE = re.compile(r"^[A-Z][A-Z0-9]{3}$")
_IDENTITY_FIELDS = frozenset(
    {
        "symbol",
        "market",
        "interval",
        "calendar",
        "currency",
        "price_basis",
        "volume_unit",
    }
)
_REFERENCE_FIELDS = frozenset(
    {"schema", "bars_sha256", "output_sha256", "identity", "calendar"}
)
_REFERENCE_CALENDAR_FIELDS = frozenset({"name", "sessions_sha256"})


def _plain_identity_value(value: Any, label: str, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be a bounded plain string")
    return value


def _validated_symbol(value: Any) -> str:
    """Return the producer symbol verbatim; validated mode never aliases it."""

    return _plain_identity_value(value, "validated symbol")


def _validated_mic(value: Any) -> str:
    mic = _plain_identity_value(value, "validated MIC", maximum=16)
    if mic != "crypto" and _MIC_RE.fullmatch(mic) is None:
        raise ValueError("validated MIC must be an exact canonical MIC")
    return mic


def _validated_reference(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Build the bounded consumer reference without changing producer schema."""

    calendar = manifest["calendar"]
    return {
        "schema": VALIDATED_REFERENCE_SCHEMA,
        "bars_sha256": manifest["bars_sha256"],
        "output_sha256": manifest["output_sha256"],
        "identity": manifest["identity"],
        "calendar": {
            "name": calendar["name"],
            "sessions_sha256": calendar["sessions_sha256"],
        },
    }


def _strict_json_object(raw: str, label: str) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 4096:
        raise ValueError(f"{label} is missing or exceeds 4096 bytes")

    def object_hook(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=object_hook)
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ValueError(f"{label} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def parse_validated_reference(raw: str) -> dict[str, Any]:
    reference = _strict_json_object(raw, "validated bars reference")
    if frozenset(reference) != _REFERENCE_FIELDS:
        raise ValueError("validated bars reference has an invalid shape")
    if reference.get("schema") != VALIDATED_REFERENCE_SCHEMA:
        raise ValueError("validated bars reference schema is unsupported")
    for field in ("bars_sha256", "output_sha256"):
        if not isinstance(reference.get(field), str) or _DIGEST_RE.fullmatch(
            reference[field]
        ) is None:
            raise ValueError(f"validated bars reference {field} is invalid")
    identity = reference.get("identity")
    if not isinstance(identity, dict) or frozenset(identity) != _IDENTITY_FIELDS:
        raise ValueError("validated bars reference identity has an invalid shape")
    for field, value in identity.items():
        _plain_identity_value(value, f"validated bars identity {field}")
    _validated_symbol(identity["symbol"])
    _validated_mic(identity["market"])
    if identity["interval"] != "1d":
        raise ValueError("validated bars reference interval must be 1d")
    calendar = reference.get("calendar")
    if not isinstance(calendar, dict) or frozenset(calendar) != _REFERENCE_CALENDAR_FIELDS:
        raise ValueError("validated bars reference calendar has an invalid shape")
    _plain_identity_value(calendar.get("name"), "validated calendar name")
    if calendar["name"] != identity["calendar"]:
        raise ValueError("validated bars reference calendar differs from identity")
    if not isinstance(calendar.get("sessions_sha256"), str) or _DIGEST_RE.fullmatch(
        calendar["sessions_sha256"]
    ) is None:
        raise ValueError("validated bars reference calendar digest is invalid")
    return reference


def validated_forecast_context(
    symbol: str,
    mic: str,
    root: str,
    raw_reference: str,
) -> dict[str, Any]:
    """Re-resolve an echoed browser reference against the accepted store."""

    from .validated_bars import ValidatedBarsStore

    exact_symbol = _validated_symbol(symbol)
    exact_mic = _validated_mic(mic)
    reference = parse_validated_reference(raw_reference)
    identity = reference["identity"]
    if identity["symbol"] != exact_symbol or identity["market"] != exact_mic:
        raise ValueError("validated bars reference identity differs from the request")
    validated = ValidatedBarsStore(root).lookup(
        exact_symbol,
        market=exact_mic,
        interval="1d",
    )
    expected = _validated_reference(validated.manifest)
    if reference != expected:
        raise ValueError("validated bars reference is stale or does not match the manifest")
    return {
        "reference": reference,
        "bars": list(validated.records),
        "calendar_sessions": list(validated.manifest["calendar"]["sessions"]),
    }


def _query_identity(query: Mapping[str, list[str]]) -> tuple[str, str]:
    raw_symbol = (query.get("symbol", [""])[0] or "").strip()
    raw_mic = (query.get("mic", [""])[0] or "").strip()
    if "@" in raw_symbol:
        symbol_part, token_mic = raw_symbol.rsplit("@", 1)
        symbol_part = symbol_part.strip()
        token_mic = token_mic.strip()
        if raw_mic and raw_mic != token_mic:
            raise ValueError("symbol token MIC conflicts with the mic parameter")
        raw_symbol, raw_mic = symbol_part, token_mic
    return raw_symbol, raw_mic


def validated_candles_payload(symbol: str, mic: str, root: str) -> dict:
    """Load one accepted daily consensus manifest into the chart wire shape."""
    from .validated_bars import ValidatedBarsStore

    exact_symbol = _validated_symbol(symbol)
    exact_mic = _validated_mic(mic)
    validated = ValidatedBarsStore(root).lookup(
        exact_symbol,
        market=exact_mic,
        interval="1d",
    )
    manifest = validated.manifest
    candles = [
        {
            "time": record["date"],
            "open": record["open"],
            "high": record["high"],
            "low": record["low"],
            "close": record["close"],
            "volume": record["volume"],
        }
        for record in validated.records
    ]
    if not candles:
        raise ValueError(f"accepted consensus manifest has no bars for {symbol}")
    return {
        "ok": True,
        "symbol": exact_symbol,
        "mic": exact_mic,
        "candles": candles,
        "provenance": {
            "data_mode": "validated",
            "bars_sha256": manifest["bars_sha256"],
            "output_sha256": manifest["output_sha256"],
            "identity": manifest["identity"],
            "reference": _validated_reference(manifest),
        },
    }


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _bar_time(ts) -> str | None:
    """Calendar day ``YYYY-MM-DD``, or None. Never calls ``ts.date()`` blindly.

    Integers (RangeIndex) are not dates. String / datetime64 values that
    already look like ISO dates are accepted. Anything else is dropped.
    """
    if ts is None:
        return None
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        return None
    if isinstance(ts, datetime):
        try:
            return ts.date().isoformat()
        except Exception:
            return None
    if isinstance(ts, date):
        try:
            return ts.isoformat()
        except Exception:
            return None
    if hasattr(ts, "to_pydatetime"):
        try:
            return _bar_time(ts.to_pydatetime())
        except Exception:
            return None
    text = str(ts).strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        chunk = text[:10]
        parts = chunk.split("-")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            try:
                return date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
            except ValueError:
                return None
    return None


def _row_bar(ts, open_, high, low, close, volume) -> dict | None:
    day = _bar_time(ts)
    if day is None:
        return None
    ohlc = (_finite(open_), _finite(high), _finite(low), _finite(close))
    if any(part is None for part in ohlc):
        return None
    vol = _finite(volume)
    if vol is None:
        vol = 0.0
    return {
        "time": day,
        "open": ohlc[0],
        "high": ohlc[1],
        "low": ohlc[2],
        "close": ohlc[3],
        "volume": vol,
    }


def _merge_bar(prev: dict, bar: dict) -> dict:
    return {
        "time": prev["time"],
        "open": prev["open"],
        "high": max(prev["high"], bar["high"]),
        "low": min(prev["low"], bar["low"]),
        "close": bar["close"],
        "volume": prev["volume"] + bar["volume"],
    }


def bars_from_ohlcv(df) -> list[dict]:
    """OHLCV frame → unique ascending finite candles.

    A non-Timestamp index must not raise. Duplicate calendar days collapse
    to one bar (first open, max high, min low, last close, summed volume).
    Column-wise walk avoids ``iterrows()`` on long Yahoo frames.
    """
    if df is None or getattr(df, "empty", True):
        return []
    try:
        index = list(df.index)
        opens = df["open"].tolist()
        highs = df["high"].tolist()
        lows = df["low"].tolist()
        closes = df["close"].tolist()
        volumes = df["volume"].tolist() if "volume" in df.columns else None
    except Exception:
        return []
    by_time: dict[str, dict] = {}
    for i, ts in enumerate(index):
        vol = 0.0 if volumes is None else volumes[i]
        bar = _row_bar(ts, opens[i], highs[i], lows[i], closes[i], vol)
        if bar is None:
            continue
        prev = by_time.get(bar["time"])
        by_time[bar["time"]] = bar if prev is None else _merge_bar(prev, bar)
    return [by_time[key] for key in sorted(by_time)]


def candles_payload(df, symbol: str) -> dict:
    """OHLCV dataframe → the lightweight-charts JSON shape.

    Dirty frames become a structured body (empty / ``ok: False``), never a
    500 from ``ts.date()`` on a non-Timestamp index.
    """
    candles = bars_from_ohlcv(df)
    if not candles:
        return {
            "ok": False,
            "error": f"no usable bars for {symbol}",
            "symbol": symbol,
            "candles": [],
        }
    return {"ok": True, "symbol": symbol, "candles": candles}


_EXTERNAL_SYMBOL_RE = re.compile(r"^SGX:([A-Z0-9]{1,12})$")
EXTERNAL_BARS_SCHEMA = "mktdaily.bars.v1"


def external_candles_payload(root: str, symbol: str) -> dict:
    """Operator-local exchange series file → the lightweight-charts shape.

    ``SGX:<CODE>`` maps to ``{root}/<CODE>.json`` holding one
    ``mktdaily.bars.v1`` document. Fail closed at every step: an
    unconfigured root, an unknown code, or a malformed file all become a
    structured ``ok: False`` body, never an exception or a fallback fetch.
    """
    if not root:
        return {
            "ok": False,
            "error": "external bars root is not configured (external_bars_root)",
            "symbol": symbol,
            "candles": [],
        }
    match = _EXTERNAL_SYMBOL_RE.fullmatch(symbol)
    if match is None:
        return {
            "ok": False,
            "error": f"invalid external symbol: {symbol}",
            "symbol": symbol,
            "candles": [],
        }
    path = Path(root).expanduser() / f"{match.group(1)}.json"
    try:
        raw = path.read_text(encoding="utf-8")
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "error": f"external bars unavailable for {symbol}: {exc}",
            "symbol": symbol,
            "candles": [],
        }
    if not isinstance(document, dict) or document.get("schema") != EXTERNAL_BARS_SCHEMA:
        return {
            "ok": False,
            "error": f"external bars for {symbol} lack the {EXTERNAL_BARS_SCHEMA} schema",
            "symbol": symbol,
            "candles": [],
        }
    bars = document.get("bars")
    if not isinstance(bars, list):
        return {
            "ok": False,
            "error": f"external bars for {symbol} have no bars list",
            "symbol": symbol,
            "candles": [],
        }
    candles_by_time: dict[str, dict] = {}
    for entry in bars:
        if not isinstance(entry, dict):
            continue
        bar = _row_bar(
            entry.get("date"),
            entry.get("open"),
            entry.get("high"),
            entry.get("low"),
            entry.get("close"),
            entry.get("volume", 0.0),
        )
        if bar is None:
            continue
        prev = candles_by_time.get(bar["time"])
        candles_by_time[bar["time"]] = bar if prev is None else _merge_bar(prev, bar)
    candles = [candles_by_time[key] for key in sorted(candles_by_time)]
    if not candles:
        return {
            "ok": False,
            "error": f"no usable external bars for {symbol}",
            "symbol": symbol,
            "candles": [],
        }
    return {
        "ok": True,
        "symbol": symbol,
        "candles": candles,
        "provenance": {
            "data_mode": "external",
            "source": str(document.get("source") or "operator-local export"),
            "name": str(document.get("name") or match.group(1)),
            "unit": str(document.get("unit") or ""),
        },
    }


def is_running(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_chart_server(port: int) -> bool:
    """Identify a listener as one of ours by its Server header.

    Reuse is read-only: the caller does not gain ownership, so its
    shutdown never terminates the shared server. The owning TUI still
    does; a later chart open after that respawns it via ensure_running.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5) as s:
            s.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            data = s.recv(2048)
    except OSError:
        return False
    head = data.split(b"\r\n\r\n", 1)[0].lower()
    return b"server: stammtisch-chart/" in head


def find_available_port(host: str = "127.0.0.1") -> int:
    """Ask the OS for an available loopback port."""
    with socket.socket() as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


# One serialized ownership registry. Startup deliberately holds this lock
# through spawn and readiness: chart launches are rare, while releasing it
# earlier lets concurrent workers overwrite ownership or lets shutdown miss a
# child between Popen and registration.
_owned_lock = threading.Lock()
_owned_process: subprocess.Popen[str] | None = None
_owned_port: int | None = None
_owned_generation = 0
_owned_shutdown = False

_READY_SCHEMA = "stammtisch.chart-ready.v1"
_READY_TIMEOUT_SECONDS = 5.0


def _reap_owned_locked() -> None:
    global _owned_process, _owned_port
    if _owned_process is not None and _owned_process.poll() is not None:
        if _owned_process.stdout is not None:
            try:
                _owned_process.stdout.close()
            except OSError:
                pass
        _owned_process = None
        _owned_port = None


def _stop_process(process: subprocess.Popen[str]) -> None:
    """Terminate one process group known to have been spawned by us."""
    try:
        if process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:  # pragma: no cover - non-POSIX only
                    process.terminate()
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:  # pragma: no cover - non-POSIX only
                        process.kill()
                except (OSError, ProcessLookupError):
                    pass
                try:
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
    finally:
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass


def _await_child_ready(
    process: subprocess.Popen[str],
    token: str,
    port: int,
) -> bool:
    """Accept readiness only from the stdout pipe of the spawned child.

    A TCP connect probe cannot establish listener ownership: another local
    process can win the bind race after the preflight check.  The child emits
    this one-time record only after its HTTPServer has bound successfully.
    """
    if process.stdout is None:
        return False
    records: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

    def _read() -> None:
        try:
            records.put(process.stdout.readline())
        except BaseException as exc:  # the parent must fail closed on pipe I/O
            records.put(exc)

    threading.Thread(target=_read, daemon=True).start()
    try:
        raw = records.get(timeout=_READY_TIMEOUT_SECONDS)
    except queue.Empty:
        return False
    if isinstance(raw, BaseException) or not raw:
        return False
    try:
        ready = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    return (
        isinstance(ready, dict)
        and ready.get("schema") == _READY_SCHEMA
        and secrets.compare_digest(str(ready.get("token", "")), token)
        and ready.get("port") == port
        and ready.get("pid") == process.pid
        and process.poll() is None
    )


def stop_owned_server() -> None:
    """Permanently close this application's chart-server lifecycle."""
    global _owned_process, _owned_port, _owned_generation, _owned_shutdown
    with _owned_lock:
        _owned_shutdown = True
        # Invalidate a worker that was queued before shutdown but has not yet
        # acquired the lock. It must not start a new child after unmount.
        _owned_generation += 1
        _reap_owned_locked()
        process = _owned_process
        _owned_process = None
        _owned_port = None
        if process is not None:
            # Keep startup serialized until the owned child is gone. A new
            # launch can never race an old listener still releasing its port.
            _stop_process(process)


def ensure_running(port: int = DEFAULT_PORT) -> int | None:
    """Return the listening port, starting the server when necessary."""
    global _owned_process, _owned_port
    try:
        selected = int(port)
    except (TypeError, ValueError):
        return None
    # This fast check is repeated under the lock. Its purpose is to keep a
    # worker that starts executing only after application shutdown from ever
    # spawning a child; a generation snapshot alone cannot identify that case.
    if _owned_shutdown:
        return None
    requested_generation = _owned_generation
    with _owned_lock:
        if _owned_shutdown or requested_generation != _owned_generation:
            return None
        _reap_owned_locked()
        if _owned_process is not None and _owned_port is not None:
            if selected <= 0 or selected == _owned_port:
                # This process reached the authenticated child-pipe handshake
                # before it entered the registry. A live PID is therefore
                # sufficient to reuse it without trusting a fresh port probe.
                return _owned_port
            # The registry intentionally owns at most one child. Starting on a
            # second requested port would overwrite the first process handle
            # and make application shutdown leak it.
            return None
        if selected > 0 and is_running(selected):
            # A listener already occupies the configured port. If its Server
            # header identifies it as a stammtisch chart server (another TUI
            # instance), reuse it without ownership: this app never terminates
            # it. A genuinely foreign listener stays fail-closed.
            return selected if _is_chart_server(selected) else None
        if selected <= 0:
            try:
                selected = find_available_port()
            except OSError:
                return None
        repo_root = Path(__file__).resolve().parent.parent
        ready_token = secrets.token_hex(32)
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "tui.chart_server",
                    "--port",
                    str(selected),
                    "--ready-token",
                    ready_token,
                    "--parent-pid",
                    str(os.getpid()),
                ],
                cwd=str(repo_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=os.name == "posix",
            )
        except OSError:
            return None
        _owned_process = process
        _owned_port = selected
        if _await_child_ready(process, ready_token, selected):
            return selected
        _owned_process = None
        _owned_port = None
        _stop_process(process)
        return None


class ChartHandler(BaseHTTPRequestHandler):
    server_version = "stammtisch-chart/1.0"

    # ── plumbing ────────────────────────────────────────────────────

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def _static(self, path: Path, content_type: str) -> None:
        if path.is_file():
            self._send(200, path.read_bytes(), content_type)
        else:
            self._send(404, b"not found", "text/plain")

    def _chart_page(self) -> None:
        """Serve the viewer with chart_math.js inlined.

        The TUI leaves a long-lived local server running. A refresh picks up
        new HTML from disk, but not new static routes — so a separate
        /static/chart_math.js request 404s, ChartMath is undefined, and
        the page never creates the chart. Inlining keeps one disk file
        as the source of truth and one response for the browser.
        """
        if not INDEX.is_file():
            self._send(404, b"not found", "text/plain")
            return
        html = INDEX.read_text(encoding="utf-8")
        math_path = STATIC_DIR / "chart_math.js"
        if math_path.is_file():
            snippet = "<script>\n" + math_path.read_text(encoding="utf-8") + "\n</script>"
            html = html.replace(
                '<script src="/static/chart_math.js"></script>',
                snippet,
                1,
            )
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        parsed = urlparse(self.path)
        route = parsed.path
        try:
            if route == "/":
                self._chart_page()
            elif route == "/chart/" or route.startswith("/chart/"):
                # The page is a single app; the symbol rides in the path.
                self._chart_page()
            elif route.startswith("/static/"):
                # Only files that already live in STATIC_DIR — basename so
                # /static/../engine.py cannot escape.
                name = Path(route).name
                allowed = {
                    "lightweight-charts.standalone.production.js",
                    "chart_math.js",
                }
                if name in allowed:
                    self._static(STATIC_DIR / name, "application/javascript")
                else:
                    self._send(404, b"not found", "text/plain")
            elif route == "/api/candles":
                self._api_candles(parse_qs(parsed.query))
            elif route == "/api/forecast":
                self._api_forecast(parse_qs(parsed.query))
            elif route == "/api/search":
                self._api_search(parse_qs(parsed.query))
            else:
                self._send(404, b"not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001 — a server must always answer
            self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    # ── API ─────────────────────────────────────────────────────────

    def _api_search(self, query: dict) -> None:
        q = (query.get("q", [""])[0] or "").strip()
        market = (query.get("market", [""])[0] or "").strip()
        self._json(200, search_payload(q, market or None))

    def _api_candles(self, query: dict) -> None:
        try:
            raw_symbol, mic = _query_identity(query)
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc), "candles": []})
            return
        start = query.get("start", ["2020-01-01"])[0]
        market = (query.get("market", ["auto"])[0] or "auto").strip().lower()
        if not raw_symbol:
            self._json(400, {"ok": False, "error": "missing symbol"})
            return
        config = Config()
        try:
            mode = config.ohlcv_mode
        except (TypeError, ValueError) as exc:
            self._json(
                200,
                {
                    "ok": False,
                    "error": str(exc),
                    "symbol": raw_symbol,
                    "mic": mic,
                    "candles": [],
                    "provenance": {"data_mode": "invalid"},
                },
            )
            return
        if mode == "validated":
            try:
                payload = validated_candles_payload(
                    raw_symbol, mic, config.validated_bars_root
                )
            except Exception as exc:  # loader failures are a closed data gate
                self._json(
                    200,
                    {
                        "ok": False,
                        "error": f"validated bars unavailable: {exc}",
                        "symbol": raw_symbol,
                        "mic": mic,
                        "candles": [],
                        "provenance": {"data_mode": "validated"},
                    },
                )
                return
            self._json(200, payload)
            return

        if raw_symbol.startswith("SGX:"):
            # Operator-local exchange series (e.g. SGX daily settlements
            # exported by a domain adapter) — never routed to quantkit.
            self._json(
                200, external_candles_payload(config.external_bars_root, raw_symbol)
            )
            return

        symbol = _normalize_symbol(raw_symbol)
        if market in ("all", "*", ""):
            market = "auto"
        if market == "cn":
            market = "cn"
        elif market == "hk":
            market = "hk"
        elif market in ("us", "jp", "kr"):
            market = "us"  # yahoo path in quantkit
        from quantkit.data import fetch_ohlcv

        df = fetch_ohlcv(symbol, market=market, start=start, data_dir=str(config.data_dir))
        if _missing_ohlcv(df):
            self._json(200, {"ok": False, "error": f"no data for {symbol}"})
            return
        self._json(200, candles_payload(df, symbol))

    def _api_forecast(self, query: dict) -> None:
        config = Config()
        try:
            raw_symbol, mic = _query_identity(query)
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        configured_horizon = config.get("kronos_horizon", 20)
        try:
            horizon = int(query.get("horizon", [configured_horizon])[0])
        except (TypeError, ValueError):
            self._json(400, {"ok": False, "error": "horizon must be an integer"})
            return
        if not 1 <= horizon <= 256:
            self._json(400, {"ok": False, "error": "horizon must be between 1 and 256"})
            return
        if not raw_symbol:
            self._json(400, {"ok": False, "error": "missing symbol"})
            return
        try:
            mode = config.ohlcv_mode
        except (TypeError, ValueError) as exc:
            self._json(200, {"ok": False, "error": str(exc)})
            return
        validated_context = None
        if mode == "validated":
            try:
                validated_context = validated_forecast_context(
                    raw_symbol,
                    mic,
                    config.validated_bars_root,
                    (query.get("validated_ref", [""])[0] or ""),
                )
            except Exception as exc:
                self._json(
                    200,
                    {
                        "ok": False,
                        "error": f"validated forecast linkage unavailable: {exc}",
                        "provenance": {"data_mode": "validated"},
                    },
                )
                return
            symbol = raw_symbol
        else:
            symbol = _normalize_symbol(raw_symbol)
        driver = TimeseriesDriver(config.get("kronos_cmd"))
        self._json(
            200,
            driver.forecast(
                symbol,
                horizon=horizon,
                validated_context=validated_context,
            ),
        )

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Keep the TUI-spawned server quiet on stderr.
        pass


_PARENT_POLL_SECONDS = 2.0


def _install_parent_death_watch(parent_pid: int | None) -> None:
    """Exit when the TUI that spawned us is gone.

    The TUI's ownership registry can only reap this process while the TUI
    is alive to run its shutdown hooks. A kill -9, a lost terminal, or an
    OOM kill skips every one of those hooks, and a chart server left behind
    then serves forever with no UI attached. Poll getppid() instead: the
    kernel reparents us the moment the TUI exits, so the poll cannot miss
    a death. (prctl(PR_SET_PDEATHSIG) is unusable here — it fires when the
    spawning *thread* ends, and the TUI starts us from a short-lived
    worker.)
    """
    if parent_pid is None or not hasattr(os, "getppid"):
        # Manually started (python -m tui.chart_server): operator-managed
        # lifecycle, no parent contract.
        return
    if os.getppid() != parent_pid:
        # The parent died between spawn and this point; do not bind at all.
        sys.exit(0)

    def _watch() -> None:
        while True:
            time.sleep(_PARENT_POLL_SECONDS)
            if os.getppid() != parent_pid:
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=_watch, name="parent-death-watch", daemon=True).start()


def run(
    port: int = DEFAULT_PORT,
    host: str = "127.0.0.1",
    ready_token: str | None = None,
    parent_pid: int | None = None,
) -> None:
    _install_parent_death_watch(parent_pid)
    server = ThreadingHTTPServer((host, port), ChartHandler)
    bound_port = int(server.server_address[1])
    if ready_token is None:
        print(
            f"stammtisch chart server on http://{host}:{bound_port} "
            "(Ctrl+C to stop)",
            flush=True,
        )
    else:
        print(
            json.dumps(
                {
                    "schema": _READY_SCHEMA,
                    "token": ready_token,
                    "port": bound_port,
                    "pid": os.getpid(),
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="STAMMTISCH K-line chart server")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ready-token", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--parent-pid", type=int, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()
    run(
        port=args.port,
        host=args.host,
        ready_token=args.ready_token,
        parent_pid=args.parent_pid,
    )
