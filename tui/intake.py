"""Fail-closed subprocess contract for a daily-data intake workspace.

The intake implementation lives outside the TUI.  This module launches a
pre-tokenized command, accepts one versioned JSON envelope on stdout, and
verifies every artifact before exposing the workspace to callers.  Artifact
content is never translated or rewritten here.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Sequence

from .subproc import run_bounded
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


INTAKE_SCHEMA = "stammtisch.daily-intake.v1"
EVIDENCE_SCHEMA = "stammtisch.daily-intake-evidence.v1"
DATASET_SCHEMA = "stammtisch.daily-dataset.v1"
REPORT_SCHEMA = "stammtisch.daily-report.v1"
MARKET_QUOTES_SCHEMA = "stammtisch.market-quotes.v1"
MARKET_CALENDAR_SCHEMA = "stammtisch.market-calendar.v1"
CONTRACT_REVISION = 1
DEFAULT_TIMEOUT_SECONDS = 900.0
MAX_TIMEOUT_SECONDS = 3600.0
ARTIFACT_KEYS = (
    "evidence_manifest",
    "canonical_dataset",
    "report_json",
    "report_html",
)
REJECTED_ARTIFACT_KEYS = ("evidence_manifest", "canonical_dataset")
EVIDENCE_COUNT_KEYS = ("expected", "succeeded", "failed", "pruned")
COUNT_KEYS = EVIDENCE_COUNT_KEYS + ("canonical_records", "sources", "markets")

_DATE_RE = re.compile(r"^20\d{6}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TERMINAL_STATUSES = frozenset(("succeeded", "failed", "pruned"))
_ACCEPTED_QUALITY_STATUSES = frozenset(("passed", "degraded"))
_RESERVED_OPTIONS = frozenset(("--workspace-root", "--date", "--json"))
_QUOTE_SYMBOLS = frozenset(("SPY", "QQQ", "DIA"))
_QUOTE_STATUSES = frozenset(("complete", "not_completed_session", "stale"))
_CALENDAR_IDENTITIES = {
    "ashare": ("XSHG", "exchange_calendars", "Asia/Shanghai"),
    "hk": ("XHKG", "exchange_calendars", "Asia/Hong_Kong"),
    "us": ("XNYS", "exchange_calendars", "America/New_York"),
    "jp": ("XTKS", "exchange_calendars", "Asia/Tokyo"),
    "kr": ("XKRX", "exchange_calendars", "Asia/Seoul"),
    "sg": ("XSES", "exchange_calendars", "Asia/Singapore"),
    "crypto": ("24/7", "built_in_continuous_calendar", "UTC"),
}
_EXCHANGE_CALENDAR_VERSION = "4.13.2"
_CALENDAR_CUTOFF_COMPLETENESS = {
    "pre_open": "partial",
    "intraday": "partial",
    "session_break": "partial",
    "post_close": "complete",
    "market_closed": "complete",
    "calendar_unavailable": "partial",
    "continuous": "complete",
}
_READINESS_COMPLETENESS = {
    "passed": "complete",
    "degraded": "partial",
    "failed": "rejected",
}
_CALENDAR_UNAVAILABLE_REASONS = frozenset(
    (
        "provider_not_installed",
        "provider_load_error",
        "calendar_out_of_bounds",
        "calendar_error",
    )
)
_CALENDAR_SNAPSHOT_KEYS = frozenset(
    ("session_date", "open", "close", "open_utc", "close_utc")
)
_CALENDAR_SESSION_KEYS = frozenset(
    (
        "schema",
        "revision",
        "market",
        "captured_at",
        "calendar_status",
        "calendar_provider",
        "calendar_version",
        "calendar_id",
        "calendar_source",
        "reason",
        "error_type",
        "exchange_timezone",
        "as_of",
        "session_date",
        "session_open",
        "session_close",
        "session_open_utc",
        "session_close_utc",
        "cutoff_type",
        "calendar_completeness",
        "is_open",
        "has_session_on_capture_date",
        "previous_session",
        "next_session",
        "completeness",
    )
)
_CALENDAR_NULL_WHEN_UNAVAILABLE = frozenset(
    (
        "exchange_timezone",
        "as_of",
        "session_date",
        "session_open",
        "session_close",
        "session_open_utc",
        "session_close_utc",
        "is_open",
        "has_session_on_capture_date",
        "previous_session",
        "next_session",
    )
)
_SECRET_FIELD_RE = re.compile(
    r"(?:^|_)(?:api_?key|authorization|credential|secret|token)(?:$|_)", re.I
)


class _ContractError(ValueError):
    pass


@dataclass(frozen=True)
class IntakeResult:
    """A verified intake workspace, or a fail-closed diagnostic result."""

    ok: bool
    workspace: Path
    date: str | None = None
    artifacts: dict[str, Path] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    envelope: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    returncode: int = 0
    stderr: str = ""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise _ContractError(f"non-finite JSON number: {value}")


def _load_json(payload: bytes | str, label: str) -> dict[str, Any]:
    try:
        if isinstance(payload, bytes):
            text = payload.decode("utf-8")
        else:
            text = payload
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _ContractError) as exc:
        raise _ContractError(f"{label} is not strict JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise _ContractError(f"{label} must be a JSON object")
    return value


def _real_date(value: Any, label: str = "date") -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise _ContractError(f"{label} must use YYYYMMDD")
    try:
        dt.datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise _ContractError(f"{label} is not a real calendar date") from exc
    return value


def _plain_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _ContractError(f"{label} must be a non-negative integer")
    return value


def _string(value: Any, label: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        suffix = "a non-empty string" if nonempty else "a string"
        raise _ContractError(f"{label} must be {suffix}")
    return value


def _version(document: Mapping[str, Any], schema: str, label: str) -> None:
    if document.get("schema") != schema:
        raise _ContractError(f"unsupported {label} schema: {document.get('schema')!r}")
    revision = document.get("revision")
    if isinstance(revision, bool) or revision != CONTRACT_REVISION:
        raise _ContractError(f"unsupported {label} revision: {revision!r}")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _ContractError(f"{label} must be an object")
    return value


def _count_mapping(value: Any, label: str) -> dict[str, int]:
    raw = _mapping(value, label)
    result: dict[str, int] = {}
    for key, count in raw.items():
        _string(key, f"{label} key")
        parsed = _plain_int(count, f"{label}.{key}")
        if parsed == 0:
            raise _ContractError(f"{label}.{key} must be positive when present")
        result[key] = parsed
    return result


def _contained_path(root: Path, value: Any, label: str) -> Path:
    raw = _string(value, f"{label}.path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _ContractError(f"{label} is missing: {candidate}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise _ContractError(f"{label} escapes the workspace: {candidate}") from exc
    if not resolved.is_file():
        raise _ContractError(f"{label} is not a regular file: {candidate}")
    return resolved


def _digest_metadata(
    root: Path,
    metadata: Any,
    label: str,
) -> tuple[Path, str, int]:
    item = _mapping(metadata, label)
    path = _contained_path(root, item.get("path"), label)
    digest = item.get("sha256")
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise _ContractError(f"{label}.sha256 must be 64 lowercase hexadecimal characters")
    expected_size = _plain_int(item.get("bytes"), f"{label}.bytes")
    payload = path.read_bytes()
    if len(payload) != expected_size:
        raise _ContractError(f"{label} byte count does not match the file")
    if hashlib.sha256(payload).hexdigest() != digest:
        raise _ContractError(f"{label} SHA-256 does not match the file")
    return path, digest, expected_size


def _same_identity(document: Mapping[str, Any], date: str, run_id: str, label: str) -> None:
    if document.get("date") != date:
        raise _ContractError(f"{label} date does not match the envelope")
    if document.get("run_id") != run_id:
        raise _ContractError(f"{label} run_id does not match the envelope")


def _lineage(
    document: Mapping[str, Any],
    output: str,
    input_name: str,
    digest: str,
) -> None:
    lineage = _mapping(document.get("lineage"), f"{output} lineage")
    edge = _mapping(lineage.get(output), f"{output} lineage edge")
    if edge.get("input") != input_name:
        raise _ContractError(f"{output} lineage must name {input_name} as its input")
    if edge.get("input_sha256") != digest:
        raise _ContractError(f"{output} lineage digest does not match {input_name}")


def _zoned_timestamp(value: Any, label: str) -> dt.datetime:
    return _aware_timestamp(value, label).astimezone(dt.timezone.utc)


def _aware_timestamp(value: Any, label: str) -> dt.datetime:
    raw = _string(value, label)
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _ContractError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _ContractError(f"{label} must include a UTC offset")
    return parsed


def _same_local_timestamp(
    observed: dt.datetime, expected: dt.datetime
) -> bool:
    return (
        observed.replace(tzinfo=None) == expected.replace(tzinfo=None)
        and observed.utcoffset() == expected.utcoffset()
    )


def _validate_quality_readiness(
    quality: Mapping[str, Any], label: str
) -> str:
    status = quality.get("status")
    if status not in _READINESS_COMPLETENESS:
        raise _ContractError(f"{label}.status is invalid")
    complete = quality.get("complete")
    if not isinstance(complete, bool) or complete != (status == "passed"):
        raise _ContractError(f"{label}.complete conflicts with status")
    return str(status)


def _validate_calendar_snapshot(
    value: Any,
    timezone: ZoneInfo,
    label: str,
) -> tuple[str, dt.datetime, dt.datetime]:
    snapshot = _mapping(value, label)
    if set(snapshot) != _CALENDAR_SNAPSHOT_KEYS:
        raise _ContractError(f"{label} has an invalid revision-1 shape")
    session_date = _string(snapshot.get("session_date"), f"{label}.session_date")
    try:
        parsed_date = dt.date.fromisoformat(session_date)
    except ValueError as exc:
        raise _ContractError(f"{label}.session_date is invalid") from exc
    opened = _aware_timestamp(snapshot.get("open"), f"{label}.open")
    closed = _aware_timestamp(snapshot.get("close"), f"{label}.close")
    opened_utc_raw = _aware_timestamp(snapshot.get("open_utc"), f"{label}.open_utc")
    closed_utc_raw = _aware_timestamp(snapshot.get("close_utc"), f"{label}.close_utc")
    if opened_utc_raw.utcoffset() != dt.timedelta(0) or closed_utc_raw.utcoffset() != dt.timedelta(0):
        raise _ContractError(f"{label} UTC timestamps must use a zero offset")
    opened_utc = opened_utc_raw.astimezone(dt.timezone.utc)
    closed_utc = closed_utc_raw.astimezone(dt.timezone.utc)
    if opened.astimezone(dt.timezone.utc) != opened_utc or closed.astimezone(dt.timezone.utc) != closed_utc:
        raise _ContractError(f"{label} local and UTC timestamps differ")
    if not _same_local_timestamp(opened, opened_utc.astimezone(timezone)) or not _same_local_timestamp(
        closed, closed_utc.astimezone(timezone)
    ):
        raise _ContractError(f"{label} timestamps differ from the exchange timezone")
    if opened.date() != parsed_date or closed.date() != parsed_date:
        raise _ContractError(f"{label} timestamps differ from the session date")
    if closed_utc <= opened_utc:
        raise _ContractError(f"{label} close must follow open")
    return session_date, opened_utc, closed_utc


def _validate_calendar_session(
    value: Any,
    quality: Mapping[str, Any],
    label: str,
) -> dict[str, Any]:
    """Validate one revision-pinned calendar verdict and readiness wrapper."""

    session = _mapping(value, label)
    if set(session) != _CALENDAR_SESSION_KEYS:
        raise _ContractError(f"{label} has an invalid revision-1 shape")
    _version(session, MARKET_CALENDAR_SCHEMA, label)
    market = _string(session.get("market"), f"{label}.market")
    identity = _CALENDAR_IDENTITIES.get(market)
    if identity is None:
        raise _ContractError(f"{label}.market is unsupported")
    expected_calendar_id, expected_provider, expected_timezone = identity
    if session.get("calendar_id") != expected_calendar_id:
        raise _ContractError(f"{label}.calendar_id is invalid for {market}")
    if session.get("calendar_provider") != expected_provider:
        raise _ContractError(f"{label}.calendar_provider is invalid for {market}")
    status = session.get("calendar_status")
    if status not in {"available", "unavailable"}:
        raise _ContractError(f"{label}.calendar_status is invalid")
    version = session.get("calendar_version")
    if version is not None:
        _string(version, f"{label}.calendar_version")
    source = _mapping(session.get("calendar_source"), f"{label}.calendar_source")
    expected_source = {
        "provider": expected_provider,
        "version": version,
        "calendar_id": expected_calendar_id,
        "status": status,
    }
    if source != expected_source:
        raise _ContractError(f"{label}.calendar_source differs from its identity fields")

    captured_raw = _aware_timestamp(session.get("captured_at"), f"{label}.captured_at")
    if captured_raw.utcoffset() != dt.timedelta(0):
        raise _ContractError(f"{label}.captured_at must use UTC")
    captured_at = captured_raw.astimezone(dt.timezone.utc)
    cutoff = session.get("cutoff_type")
    expected_calendar_completeness = _CALENDAR_CUTOFF_COMPLETENESS.get(cutoff)
    if expected_calendar_completeness is None:
        raise _ContractError(f"{label}.cutoff_type is invalid")
    if session.get("calendar_completeness") != expected_calendar_completeness:
        raise _ContractError(f"{label}.calendar_completeness conflicts with cutoff_type")

    quality_status = _validate_quality_readiness(quality, f"{label} quality")
    expected_readiness = _READINESS_COMPLETENESS[quality_status]
    if session.get("completeness") != expected_readiness:
        raise _ContractError(f"{label}.completeness conflicts with quality status")
    if expected_calendar_completeness == "partial" and quality_status == "passed":
        raise _ContractError(f"{label} cannot pass with a partial calendar verdict")

    if status == "unavailable":
        if market == "crypto" or cutoff != "calendar_unavailable":
            raise _ContractError(f"{label} unavailable state is invalid")
        if session.get("reason") not in _CALENDAR_UNAVAILABLE_REASONS:
            raise _ContractError(f"{label}.reason is invalid for an unavailable calendar")
        reason = session.get("reason")
        if reason == "provider_not_installed":
            if version is not None:
                raise _ContractError(
                    f"{label}.calendar_version must be null when the provider is absent"
                )
        elif version != _EXCHANGE_CALENDAR_VERSION:
            raise _ContractError(
                f"{label}.calendar_version is unsupported for revision 1"
            )
        error_type = session.get("error_type")
        if error_type is not None:
            _string(error_type, f"{label}.error_type")
        if any(session.get(key) is not None for key in _CALENDAR_NULL_WHEN_UNAVAILABLE):
            raise _ContractError(f"{label} unavailable state exposes uncertified session data")
        return session

    if version is None:
        raise _ContractError(f"{label}.calendar_version is required when available")
    if session.get("reason") is not None or session.get("error_type") is not None:
        raise _ContractError(f"{label} available state cannot carry an error")
    if cutoff == "calendar_unavailable":
        raise _ContractError(f"{label} available state has an unavailable cutoff")

    if market == "crypto":
        if (
            version != "1"
            or cutoff != "continuous"
            or session.get("exchange_timezone") != "UTC"
            or session.get("is_open") is not True
            or session.get("has_session_on_capture_date") is not True
            or session.get("previous_session") is not None
            or session.get("next_session") is not None
        ):
            raise _ContractError(f"{label} continuous calendar state is invalid")
        for key in (
            "session_open",
            "session_close",
            "session_open_utc",
            "session_close_utc",
        ):
            if session.get(key) is not None:
                raise _ContractError(f"{label}.{key} must be null for a continuous market")
        as_of = _aware_timestamp(session.get("as_of"), f"{label}.as_of")
        if as_of.astimezone(dt.timezone.utc) != captured_at:
            raise _ContractError(f"{label}.as_of differs from captured_at")
        if session.get("session_date") != captured_at.date().isoformat():
            raise _ContractError(f"{label}.session_date differs from captured_at")
        return session

    if version != _EXCHANGE_CALENDAR_VERSION:
        raise _ContractError(
            f"{label}.calendar_version is unsupported for revision 1"
        )

    if cutoff == "continuous":
        raise _ContractError(f"{label} exchange calendar cannot be continuous")
    if session.get("exchange_timezone") != expected_timezone:
        raise _ContractError(f"{label}.exchange_timezone is invalid for {market}")
    try:
        timezone = ZoneInfo(expected_timezone)
    except Exception as exc:  # pragma: no cover - shipped zones are mandatory
        raise _ContractError(f"{label}.exchange_timezone is unavailable") from exc
    as_of = _aware_timestamp(session.get("as_of"), f"{label}.as_of")
    expected_as_of = captured_at.astimezone(timezone)
    if as_of.astimezone(dt.timezone.utc) != captured_at or not _same_local_timestamp(
        as_of, expected_as_of
    ):
        raise _ContractError(f"{label}.as_of differs from captured_at")
    is_open = session.get("is_open")
    has_session = session.get("has_session_on_capture_date")
    if not isinstance(is_open, bool) or not isinstance(has_session, bool):
        raise _ContractError(f"{label} exchange state flags must be booleans")
    previous = _mapping(session.get("previous_session"), f"{label}.previous_session")
    next_session = _mapping(session.get("next_session"), f"{label}.next_session")
    previous_date, previous_open, previous_close = _validate_calendar_snapshot(
        previous, timezone, f"{label}.previous_session"
    )
    next_date, next_open, _ = _validate_calendar_snapshot(
        next_session, timezone, f"{label}.next_session"
    )
    if previous_open > next_open or previous_date > next_date:
        raise _ContractError(f"{label} adjacent sessions are out of order")
    selected = {
        "session_date": session.get("session_date"),
        "open": session.get("session_open"),
        "close": session.get("session_close"),
        "open_utc": session.get("session_open_utc"),
        "close_utc": session.get("session_close_utc"),
    }
    if selected != previous:
        raise _ContractError(f"{label} selected session differs from previous_session")

    local_date = expected_as_of.date().isoformat()
    within_session_span = (
        previous_date == next_date and previous_open <= captured_at < previous_close
    )
    if within_session_span:
        expected_cutoff = "intraday" if is_open else "session_break"
    elif captured_at < next_open and next_date == local_date:
        expected_cutoff = "pre_open"
    elif captured_at >= previous_close and previous_date == local_date:
        expected_cutoff = "post_close"
    else:
        expected_cutoff = "market_closed"
    if cutoff != expected_cutoff:
        raise _ContractError(f"{label}.cutoff_type misrepresents the certified sessions")
    if is_open != (cutoff == "intraday"):
        raise _ContractError(f"{label}.is_open conflicts with cutoff_type")
    if has_session != (previous_date == local_date or next_date == local_date):
        raise _ContractError(f"{label}.has_session_on_capture_date is invalid")
    return session


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise _ContractError(f"{label} must be a positive finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise _ContractError(f"{label} must be a positive finite number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise _ContractError(f"{label} must be a positive finite number")
    return parsed


def _reject_quote_secrets(value: Any, label: str = "market quote baseline") -> None:
    """Keep credentials and credential-bearing endpoint URLs out of artifacts."""

    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and _SECRET_FIELD_RE.search(key):
                raise _ContractError(f"{label} contains credential-like field {key!r}")
            _reject_quote_secrets(child, label)
    elif isinstance(value, list):
        for child in value:
            _reject_quote_secrets(child, label)
    elif isinstance(value, str) and re.search(
        r"(?:[?&](?:token|api_?key)=|\bBearer\s+)", value, re.I
    ):
        raise _ContractError(f"{label} contains credential-like text")


def _reject_quote_evidence_secrets(payload: bytes, label: str) -> None:
    text = payload.decode("utf-8", errors="replace")
    if re.search(r"(?:[?&](?:token|api_?key)=|\bBearer\s+)", text, re.I):
        raise _ContractError(f"{label} contains credential-like text")


def _validate_quote_baseline(
    baseline: dict[str, Any], root: Path
) -> set[Path]:
    """Verify optional Finnhub ETF-proxy data and its raw response evidence."""

    _reject_quote_secrets(baseline)
    expected_scalars = {
        "schema": MARKET_QUOTES_SCHEMA,
        "provider": "finnhub",
        "market": "us",
        "purpose": "latest_completed_session_cross_check",
        "non_blocking": True,
        "instrument_type": "ETF proxy",
        "official_index_close": False,
        "exchange_timezone": "America/New_York",
        "proxy": "disabled",
    }
    for key, expected in expected_scalars.items():
        if baseline.get(key) != expected:
            raise _ContractError(f"market quote baseline {key} is invalid")
    captured_at = _zoned_timestamp(
        baseline.get("captured_at"), "market quote baseline captured_at"
    )
    raw_attempts = baseline.get("attempts")
    raw_quotes = baseline.get("quotes")
    if not isinstance(raw_attempts, list) or not isinstance(raw_quotes, list):
        raise _ContractError("market quote baseline attempts and quotes must be lists")

    evidence_paths: set[Path] = set()
    attempts: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {}
    for index, raw_attempt in enumerate(raw_attempts):
        label = f"market quote attempt {index}"
        attempt = _mapping(raw_attempt, label)
        if attempt.get("provider") != "finnhub" or attempt.get("proxy") != "disabled":
            raise _ContractError(f"{label} provider or proxy declaration is invalid")
        symbol = _string(attempt.get("symbol"), f"{label}.symbol")
        if symbol not in _QUOTE_SYMBOLS or symbol in attempts:
            raise _ContractError(f"{label}.symbol is unsupported or duplicated")
        if _zoned_timestamp(attempt.get("captured_at"), f"{label}.captured_at") != captured_at:
            raise _ContractError(f"{label}.captured_at differs from the baseline")
        status = attempt.get("status")
        if status not in _QUOTE_STATUSES | {"failed"}:
            raise _ContractError(f"{label}.status is invalid")
        raw_document: dict[str, Any] | None = None
        if "evidence_ref" in attempt:
            path, _, _ = _digest_metadata(
                root, attempt["evidence_ref"], f"{label}.evidence_ref"
            )
            evidence_payload = path.read_bytes()
            _reject_quote_evidence_secrets(
                evidence_payload, f"{label}.evidence_ref response"
            )
            if path in evidence_paths:
                raise _ContractError("multiple quote attempts reference one evidence file")
            evidence_paths.add(path)
            if status != "failed":
                raw_document = _load_json(
                    evidence_payload, f"{label}.evidence_ref response"
                )
                _reject_quote_secrets(
                    raw_document, f"{label}.evidence_ref response"
                )
        elif status != "failed":
            raise _ContractError(f"{label} lacks raw provider evidence")
        attempts[symbol] = (attempt, raw_document)

    complete_count = 0
    seen_quotes: set[str] = set()
    try:
        new_york = ZoneInfo("America/New_York")
    except Exception as exc:
        # tzdata-less hosts (stripped containers, Windows without the
        # tzdata wheel): degrade to a contract error instead of crashing.
        raise _ContractError("market quote timezone database is unavailable") from exc
    for index, raw_quote in enumerate(raw_quotes):
        label = f"market quote {index}"
        quote = _mapping(raw_quote, label)
        if quote.get("provider") != "finnhub" or quote.get("proxy") != "disabled":
            raise _ContractError(f"{label} provider or proxy declaration is invalid")
        if quote.get("instrument_type") != "ETF proxy" or "close" in quote:
            raise _ContractError(f"{label} must remain an ETF proxy observation")
        symbol = _string(quote.get("symbol"), f"{label}.symbol")
        if symbol not in attempts or symbol in seen_quotes:
            raise _ContractError(f"{label}.symbol lacks one unique attempt")
        seen_quotes.add(symbol)
        attempt, attempt_raw = attempts[symbol]
        if attempt_raw is None:
            raise _ContractError(f"{label} lacks raw provider evidence")
        quote_path, quote_digest, quote_size = _digest_metadata(
            root, quote.get("evidence_ref"), f"{label}.evidence_ref"
        )
        quote_raw = _load_json(
            quote_path.read_bytes(), f"{label}.evidence_ref response"
        )
        _reject_quote_secrets(quote_raw, f"{label}.evidence_ref response")
        attempt_ref = _mapping(attempt.get("evidence_ref"), f"{label} attempt evidence_ref")
        if (
            quote_path != _contained_path(root, attempt_ref.get("path"), f"{label} attempt evidence_ref")
            or quote_digest != attempt_ref.get("sha256")
            or quote_size != attempt_ref.get("bytes")
            or quote_raw != attempt_raw
        ):
            raise _ContractError(f"{label} does not bind its attempt evidence")
        if quote.get("tracks") is None:
            raise _ContractError(f"{label}.tracks is required")
        if quote.get("exchange_timezone") != "America/New_York":
            raise _ContractError(f"{label}.exchange_timezone is invalid")
        if _zoned_timestamp(quote.get("captured_at"), f"{label}.captured_at") != captured_at:
            raise _ContractError(f"{label}.captured_at differs from the baseline")
        provider_time = _zoned_timestamp(
            quote.get("provider_timestamp"), f"{label}.provider_timestamp"
        )
        if provider_time > captured_at:
            raise _ContractError(f"{label}.provider_timestamp is in the future")
        try:
            if isinstance(attempt_raw.get("t"), bool):
                raise TypeError
            raw_timestamp = int(attempt_raw.get("t"))
        except (TypeError, ValueError, OSError, OverflowError) as exc:
            raise _ContractError(f"{label} raw timestamp is invalid") from exc
        try:
            raw_provider_time = dt.datetime.fromtimestamp(
                raw_timestamp, tz=dt.timezone.utc
            )
        except (OSError, OverflowError, ValueError) as exc:
            raise _ContractError(f"{label} raw timestamp is invalid") from exc
        if raw_provider_time != provider_time:
            raise _ContractError(f"{label} provider timestamp differs from raw evidence")
        for quote_key, raw_key in (
            ("current", "c"),
            ("previous_close", "pc"),
            ("open", "o"),
            ("high", "h"),
            ("low", "l"),
        ):
            if _positive_number(quote.get(quote_key), f"{label}.{quote_key}") != _positive_number(
                attempt_raw.get(raw_key), f"{label} raw {raw_key}"
            ):
                raise _ContractError(f"{label}.{quote_key} differs from raw evidence")
        if float(quote["high"]) < float(quote["low"]):
            raise _ContractError(f"{label}.high is below low")
        provider_local = provider_time.astimezone(new_york)
        captured_local = captured_at.astimezone(new_york)
        if quote.get("session_date") != provider_local.date().isoformat():
            raise _ContractError(f"{label}.session_date differs from provider evidence")
        delay_seconds = int((captured_at - provider_time).total_seconds())
        if quote.get("delay_seconds") != delay_seconds:
            raise _ContractError(f"{label}.delay_seconds is invalid")
        delay_status = (
            "near_real_time"
            if delay_seconds <= 20 * 60
            else "delayed"
            if delay_seconds <= 5 * 24 * 60 * 60
            else "stale"
        )
        if quote.get("delay_status") != delay_status:
            raise _ContractError(f"{label}.delay_status is invalid")
        close_window = dt.time(15, 55) <= provider_local.time() <= dt.time(16, 15)
        completed = close_window and (
            provider_local.date() < captured_local.date()
            or (
                provider_local.date() == captured_local.date()
                and captured_local.time() >= dt.time(16, 0)
            )
        )
        expected_status = (
            "stale"
            if delay_status == "stale"
            else "complete"
            if completed
            else "not_completed_session"
        )
        if quote.get("status") != expected_status or attempt.get("status") != expected_status:
            raise _ContractError(
                f"{label} status misrepresents the provider observation cutoff"
            )
        complete_count += expected_status == "complete"

    expected_baseline_status = (
        "complete"
        if attempts and complete_count == len(attempts)
        else "partial"
        if complete_count
        else "stale"
        if raw_quotes and all(
            isinstance(quote, dict) and quote.get("status") == "stale"
            for quote in raw_quotes
        )
        else "not_completed_session"
        if raw_quotes
        else "unavailable"
    )
    if baseline.get("status") != expected_baseline_status:
        raise _ContractError("market quote baseline status does not match its quotes")
    return evidence_paths


def _manifest_quote_baseline(document: Mapping[str, Any]) -> dict[str, Any] | None:
    singular_present = "market_quote_baseline" in document
    plural_present = "market_quote_baselines" in document
    if singular_present and plural_present:
        raise _ContractError("evidence manifest has ambiguous market quote baselines")
    if singular_present:
        return _mapping(document["market_quote_baseline"], "manifest market quote baseline")
    if plural_present:
        baselines = _mapping(document["market_quote_baselines"], "manifest market quote baselines")
        unsupported = set(baselines) - {"us"}
        if unsupported:
            raise _ContractError("manifest contains unsupported market quote baselines")
        if "us" in baselines:
            return _mapping(baselines["us"], "manifest US market quote baseline")
    return None


def _document_quote_baseline(document: Mapping[str, Any], label: str) -> dict[str, Any] | None:
    if "market_quote_baseline" not in document:
        return None
    return _mapping(document["market_quote_baseline"], f"{label} market quote baseline")


def _same_calendar_layer(
    document: Mapping[str, Any],
    key: str,
    expected: Mapping[str, Any],
    label: str,
) -> None:
    observed = _mapping(document.get(key), f"{label}.{key}")
    if observed != expected:
        raise _ContractError(f"{label}.{key} calendar metadata differs from the envelope")


def _validate_embedded_calendar_sessions(
    manifest: Mapping[str, Any], sessions: Mapping[str, Mapping[str, Any]]
) -> None:
    """Bind per-capture and per-attempt cutoff fields to their child session."""

    for collection in ("captures", "raw_attempts"):
        raw_items = manifest.get(collection)
        if not isinstance(raw_items, list):
            raise _ContractError(f"evidence manifest.{collection} must be a list")
        for index, raw_item in enumerate(raw_items):
            label = f"evidence manifest.{collection}[{index}]"
            item = _mapping(raw_item, label)
            market = item.get("market")
            expected = sessions.get(market) if isinstance(market, str) else None
            if expected is None:
                raise _ContractError(f"{label}.market lacks a bound session")
            missing = _CALENDAR_SESSION_KEYS - set(item)
            if missing:
                raise _ContractError(
                    f"{label} lacks embedded calendar metadata: "
                    + ", ".join(sorted(missing))
                )
            observed = {key: item[key] for key in _CALENDAR_SESSION_KEYS}
            if observed != expected:
                raise _ContractError(
                    f"{label} calendar metadata differs from its market session"
                )


def _validate_single_session_layers(
    envelope: Mapping[str, Any],
    manifest: Mapping[str, Any],
    canonical: Mapping[str, Any],
    report: Mapping[str, Any] | None,
) -> None:
    quality = _mapping(envelope.get("quality"), "envelope quality")
    session = _validate_calendar_session(
        envelope.get("session"), quality, "envelope.session"
    )
    for document, label in (
        (manifest, "evidence manifest"),
        (canonical, "canonical dataset"),
    ):
        _same_calendar_layer(document, "session", session, label)
    if report is not None:
        _same_calendar_layer(report, "session", session, "report JSON")
    _validate_embedded_calendar_sessions(
        manifest, {str(session["market"]): session}
    )

    nested_quality_session = quality.get("session")
    if nested_quality_session is not None and nested_quality_session != session:
        raise _ContractError(
            "envelope quality.session calendar metadata differs from the envelope"
        )
    for key in _CALENDAR_SESSION_KEYS:
        if key in quality and quality[key] != session[key]:
            raise _ContractError(
                f"envelope quality.{key} calendar metadata differs from the envelope"
            )
    canonical_quality = _mapping(
        canonical.get("session_quality"), "canonical dataset.session_quality"
    )
    if canonical_quality != quality:
        raise _ContractError(
            "canonical dataset.session_quality differs from the envelope quality"
        )
    if report is not None:
        report_quality = _mapping(
            report.get("session_quality"), "report JSON.session_quality"
        )
        if report_quality != quality:
            raise _ContractError(
                "report JSON.session_quality differs from the envelope quality"
            )

    raw_markets = envelope.get("session_markets")
    if raw_markets is not None and raw_markets != [session["market"]]:
        raise _ContractError("envelope.session_markets differs from its session")
    captured_at = session["captured_at"]
    timestamp_layers: list[tuple[Mapping[str, Any], str]] = [
        (manifest, "evidence manifest"),
        (canonical, "canonical dataset"),
    ]
    if report is not None:
        timestamp_layers.append((report, "report JSON"))
    for document, label in timestamp_layers:
        if document.get("captured_at") != captured_at:
            raise _ContractError(f"{label}.captured_at differs from its session")


def _validate_assembled_session_layers(
    envelope: Mapping[str, Any],
    manifest: Mapping[str, Any],
    canonical: Mapping[str, Any],
    report: Mapping[str, Any] | None,
) -> None:
    quality = _mapping(envelope.get("quality"), "envelope quality")
    _validate_quality_readiness(quality, "envelope quality")
    envelope_sessions = _mapping(
        envelope.get("market_sessions"), "envelope.market_sessions"
    )
    if not envelope_sessions:
        raise _ContractError("envelope.market_sessions must not be empty")
    for document, label in (
        (manifest, "evidence manifest"),
        (canonical, "canonical dataset"),
    ):
        _same_calendar_layer(document, "market_sessions", envelope_sessions, label)
    if report is not None:
        _same_calendar_layer(
            report, "market_sessions", envelope_sessions, "report JSON"
        )
    _validate_embedded_calendar_sessions(manifest, envelope_sessions)

    nested_market_sessions = quality.get("market_sessions")
    if nested_market_sessions is not None and nested_market_sessions != envelope_sessions:
        raise _ContractError(
            "envelope quality.market_sessions calendar metadata differs from the envelope"
        )
    canonical_quality = _mapping(
        canonical.get("session_quality"), "canonical dataset.session_quality"
    )
    if canonical_quality != quality:
        raise _ContractError(
            "canonical dataset.session_quality differs from the envelope quality"
        )
    if report is not None:
        report_quality = _mapping(
            report.get("session_quality"), "report JSON.session_quality"
        )
        if report_quality != quality:
            raise _ContractError(
                "report JSON.session_quality differs from the envelope quality"
            )

    raw_results = manifest.get("sessions")
    if not isinstance(raw_results, list) or not raw_results:
        raise _ContractError("evidence manifest.sessions must be a non-empty list")
    result_by_market: dict[str, dict[str, Any]] = {}
    for index, raw_result in enumerate(raw_results):
        label = f"evidence manifest.sessions[{index}]"
        result = _mapping(raw_result, label)
        market = _string(result.get("market"), f"{label}.market")
        if market in result_by_market or market not in envelope_sessions:
            raise _ContractError(f"{label}.market is duplicated or unexpected")
        child_quality = _mapping(result.get("quality"), f"{label}.quality")
        child_status = _validate_quality_readiness(child_quality, f"{label}.quality")
        accepted = result.get("accepted")
        if not isinstance(accepted, bool) or accepted != (
            child_status in _ACCEPTED_QUALITY_STATUSES
        ):
            raise _ContractError(f"{label}.accepted conflicts with child quality")
        child_session = _validate_calendar_session(
            result.get("session"), child_quality, f"{label}.session"
        )
        if child_session.get("market") != market:
            raise _ContractError(f"{label}.session market differs from its key")
        if child_session != envelope_sessions[market]:
            raise _ContractError(
                f"{label}.session calendar metadata differs from market_sessions"
            )
        nested_child_session = child_quality.get("session")
        if nested_child_session is not None and nested_child_session != child_session:
            raise _ContractError(
                f"{label}.quality.session calendar metadata differs from its session"
            )
        for key in _CALENDAR_SESSION_KEYS:
            if key in child_quality and child_quality[key] != child_session[key]:
                raise _ContractError(
                    f"{label}.quality.{key} differs from its session"
                )
        result_by_market[market] = result

    if set(result_by_market) != set(envelope_sessions):
        raise _ContractError(
            "evidence manifest.sessions markets differ from market_sessions"
        )
    quality_results = quality.get("sessions")
    if quality_results is not None and quality_results != raw_results:
        raise _ContractError(
            "envelope quality.sessions differs from the evidence manifest"
        )
    raw_markets = envelope.get("session_markets")
    if not isinstance(raw_markets, list) or raw_markets != list(envelope_sessions):
        raise _ContractError(
            "envelope.session_markets differs from market_sessions"
        )


def _validate_calendar_layers(
    envelope: Mapping[str, Any],
    manifest: Mapping[str, Any],
    canonical: Mapping[str, Any],
    report: Mapping[str, Any] | None,
) -> None:
    """Bind calendar metadata through all producer-owned artifact layers."""

    mode = envelope.get("mode")
    assembly_present = any(
        "market_sessions" in document for document in (envelope, manifest, canonical)
    ) or (report is not None and "market_sessions" in report)
    single_present = any(
        "session" in document for document in (envelope, manifest, canonical)
    ) or (report is not None and "session" in report)
    if assembly_present or mode == "market_session_assembly":
        if single_present:
            raise _ContractError(
                "assembled intake cannot expose an ambiguous single session"
            )
        _validate_assembled_session_layers(envelope, manifest, canonical, report)
        return
    if single_present or mode == "firecrawl":
        _validate_single_session_layers(envelope, manifest, canonical, report)


def _evidence_counts(value: Any, label: str) -> dict[str, int]:
    raw = _mapping(value, label)
    return {key: _plain_int(raw.get(key), f"{label}.{key}") for key in EVIDENCE_COUNT_KEYS}


def _validate_manifest(
    document: dict[str, Any],
    root: Path,
    date: str,
    run_id: str,
) -> tuple[dict[str, int], dict[str, tuple[Path, str]]]:
    _version(document, EVIDENCE_SCHEMA, "evidence manifest")
    _same_identity(document, date, run_id, "evidence manifest")
    if document.get("append_only") is not True:
        raise _ContractError("evidence manifest must declare append_only=true")
    captures = document.get("captures")
    if not isinstance(captures, list) or not captures:
        raise _ContractError("evidence manifest captures must be a non-empty list")

    states: Counter[str] = Counter()
    successful: dict[str, tuple[Path, str]] = {}
    seen: set[str] = set()
    evidence_paths: set[Path] = set()
    for index, raw_capture in enumerate(captures):
        label = f"evidence capture {index}"
        capture = _mapping(raw_capture, label)
        source = capture.get("source")
        if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
            raise _ContractError(f"{label}.source is invalid")
        if source in seen:
            raise _ContractError(f"duplicate terminal evidence source: {source}")
        seen.add(source)
        status = capture.get("status")
        if status not in _TERMINAL_STATUSES:
            raise _ContractError(f"{label}.status is not terminal")
        url = _string(capture.get("url"), f"{label}.url")
        parsed_url = urlsplit(url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            raise _ContractError(f"{label}.url must be an absolute HTTP(S) URL")
        _string(capture.get("market"), f"{label}.market")
        _string(capture.get("phase"), f"{label}.phase")
        states[status] += 1
        has_evidence = any(key in capture for key in ("path", "sha256", "bytes"))
        if status == "succeeded":
            path, digest, _ = _digest_metadata(root, capture, label)
            if path in evidence_paths:
                raise _ContractError(f"multiple captures reference one evidence file: {path}")
            evidence_paths.add(path)
            successful[source] = (path, digest)
        elif has_evidence:
            raise _ContractError(f"{label} must not claim evidence after {status}")

    observed = {
        "expected": len(captures),
        "succeeded": states["succeeded"],
        "failed": states["failed"],
        "pruned": states["pruned"],
    }
    declared = _evidence_counts(document.get("counts"), "evidence manifest counts")
    if declared != observed:
        raise _ContractError("evidence manifest counts do not match terminal captures")
    if declared["expected"] != sum(declared[key] for key in _TERMINAL_STATUSES):
        raise _ContractError("evidence manifest terminal counts do not add up")
    if not successful:
        raise _ContractError("evidence manifest has no successful immutable capture")
    return declared, successful


def _validate_dataset(
    document: dict[str, Any],
    root: Path,
    date: str,
    run_id: str,
    manifest_digest: str,
    evidence_counts: dict[str, int],
    successful: dict[str, tuple[Path, str]],
) -> tuple[dict[str, dict[str, Any]], dict[str, int], dict[str, int]]:
    _version(document, DATASET_SCHEMA, "canonical dataset")
    _same_identity(document, date, run_id, "canonical dataset")
    _lineage(document, "evidence_manifest", "evidence_manifest", manifest_digest)
    if _evidence_counts(document.get("evidence_counts"), "canonical evidence counts") != evidence_counts:
        raise _ContractError("canonical evidence counts do not match the manifest")

    records = document.get("records")
    if not isinstance(records, list) or not records:
        raise _ContractError("canonical dataset records must be a non-empty list")
    identities: dict[str, dict[str, Any]] = {}
    source_counter: Counter[str] = Counter()
    market_counter: Counter[str] = Counter()
    for index, raw_record in enumerate(records):
        label = f"canonical record {index}"
        record = _mapping(raw_record, label)
        record_id = _string(record.get("id"), f"{label}.id")
        if record_id in identities:
            raise _ContractError(f"duplicate canonical record id: {record_id}")
        market = _string(record.get("market"), f"{label}.market")
        market_counter[market] += 1
        title = _string(record.get("title"), f"{label}.title")
        _string(record.get("summary"), f"{label}.summary")
        url = _string(record.get("url"), f"{label}.url")
        sources = record.get("sources")
        if not isinstance(sources, list) or not sources:
            raise _ContractError(f"{label}.sources must be a non-empty list")
        normalized_sources: list[str] = []
        for source in sources:
            if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
                raise _ContractError(f"{label}.sources contains an invalid source")
            if source in normalized_sources:
                raise _ContractError(f"{label}.sources contains a duplicate source")
            if source not in successful:
                raise _ContractError(f"{label} cites a source without successful evidence: {source}")
            normalized_sources.append(source)
            source_counter[source] += 1

        references = record.get("evidence_refs")
        if not isinstance(references, list) or not references:
            raise _ContractError(f"{label}.evidence_refs must be a non-empty list")
        referenced_sources: set[str] = set()
        normalized_references: list[dict[str, str]] = []
        url_evidenced = False
        for ref_index, raw_ref in enumerate(references):
            ref_label = f"{label}.evidence_refs[{ref_index}]"
            ref = _mapping(raw_ref, ref_label)
            source = _string(ref.get("source"), f"{ref_label}.source")
            if source in referenced_sources:
                raise _ContractError(f"{label} contains duplicate evidence references")
            referenced_sources.add(source)
            expected = successful.get(source)
            if expected is None:
                raise _ContractError(f"{ref_label} names a source without successful evidence")
            path = _contained_path(root, ref.get("path"), ref_label)
            digest = ref.get("sha256")
            if path != expected[0] or digest != expected[1]:
                raise _ContractError(f"{ref_label} does not bind the source capture")
            if url.encode("utf-8") in path.read_bytes():
                url_evidenced = True
            normalized_references.append(
                {"source": source, "path": str(ref.get("path")), "sha256": str(digest)}
            )
        if not set(normalized_sources).issubset(referenced_sources):
            raise _ContractError(f"{label} lacks evidence for one or more cited sources")
        if not url_evidenced:
            raise _ContractError(f"{label} URL is absent from its referenced evidence")
        identities[record_id] = {
            "market": market,
            "title": title,
            "url": url,
            "sources": normalized_sources,
            "evidence_refs": normalized_references,
        }

    declared_sources = _count_mapping(document.get("source_counts"), "canonical source_counts")
    declared_markets = _count_mapping(document.get("market_counts"), "canonical market_counts")
    if declared_sources != dict(sorted(source_counter.items())):
        raise _ContractError("canonical source_counts do not match records")
    if declared_markets != dict(sorted(market_counter.items())):
        raise _ContractError("canonical market_counts do not match records")
    return identities, declared_sources, declared_markets


def _validate_report(
    document: dict[str, Any],
    date: str,
    run_id: str,
    canonical_digest: str,
    identities: dict[str, dict[str, Any]],
    source_counts: dict[str, int],
    market_counts: dict[str, int],
    evidence_counts: dict[str, int],
) -> None:
    _version(document, REPORT_SCHEMA, "report JSON")
    _same_identity(document, date, run_id, "report JSON")
    _lineage(document, "canonical_dataset", "canonical_dataset", canonical_digest)
    if _count_mapping(document.get("source_counts"), "report source_counts") != source_counts:
        raise _ContractError("report source_counts do not match the canonical dataset")
    if _count_mapping(document.get("market_counts"), "report market_counts") != market_counts:
        raise _ContractError("report market_counts do not match the canonical dataset")
    if _evidence_counts(document.get("intake"), "report intake counts") != evidence_counts:
        raise _ContractError("report intake counts do not match the evidence manifest")

    markets = _mapping(document.get("markets"), "report markets")
    report_identities: dict[str, dict[str, Any]] = {}
    for market, raw_items in markets.items():
        _string(market, "report market key")
        if not isinstance(raw_items, list):
            raise _ContractError(f"report market {market} must be a list")
        for index, raw_item in enumerate(raw_items):
            item = _mapping(raw_item, f"report market {market} item {index}")
            record_id = _string(item.get("id"), f"report market {market} item {index}.id")
            if record_id in report_identities:
                raise _ContractError(f"duplicate report record id: {record_id}")
            sources = item.get("sources")
            references = item.get("evidence_refs")
            if not isinstance(sources, list) or not all(isinstance(value, str) for value in sources):
                raise _ContractError(f"report record {record_id} sources must be a list of strings")
            if not isinstance(references, list) or not all(isinstance(value, dict) for value in references):
                raise _ContractError(f"report record {record_id} evidence_refs must be a list of objects")
            report_identities[record_id] = {
                "market": market,
                "title": _string(item.get("title"), f"report record {record_id}.title"),
                "url": _string(item.get("url"), f"report record {record_id}.url"),
                "sources": sources,
                "evidence_refs": references,
            }
    if report_identities != identities:
        raise _ContractError("report source identities differ from the canonical dataset")


class _ReportDigestParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.digests: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "meta":
            return
        values = {key.casefold(): value for key, value in attrs}
        if values.get("name") == "stammtisch-input-sha256" and values.get("content") is not None:
            self.digests.append(str(values["content"]))


def _validate_html(path: Path, report_digest: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _ContractError("report HTML must be valid UTF-8") from exc
    parser = _ReportDigestParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        raise _ContractError(f"report HTML cannot be parsed: {exc}") from exc
    if parser.digests != [report_digest]:
        raise _ContractError("report HTML does not bind exactly one matching report JSON digest")


def _validate_success(
    envelope: dict[str, Any],
    configured_root: Path,
    requested_date: str | None,
) -> IntakeResult:
    _version(envelope, INTAKE_SCHEMA, "intake envelope")
    if envelope.get("ok") is not True:
        raise _ContractError("intake envelope did not declare ok=true")
    quality = _mapping(envelope.get("quality"), "envelope quality")
    quality_status = quality.get("status")
    if quality_status not in _ACCEPTED_QUALITY_STATUSES:
        raise _ContractError(
            f"envelope quality status is not accepted: {quality_status!r}"
        )
    if "complete" in quality:
        _validate_quality_readiness(quality, "envelope quality")
    date = _real_date(envelope.get("date"), "envelope date")
    if requested_date is not None and date != requested_date:
        raise _ContractError("envelope date does not match the requested date")
    run_id = _string(envelope.get("run_id"), "envelope run_id")

    root_value = _string(envelope.get("workspace_root"), "envelope workspace_root")
    envelope_root = Path(root_value).expanduser().resolve(strict=True)
    if not envelope_root.is_dir() or envelope_root != configured_root:
        raise _ContractError("envelope workspace_root does not match the configured workspace")
    if "workspace_path" in envelope:
        alias = Path(_string(envelope["workspace_path"], "envelope workspace_path")).expanduser()
        if alias.resolve(strict=True) != configured_root:
            raise _ContractError("envelope workspace_path does not match workspace_root")

    raw_artifacts = _mapping(envelope.get("artifacts"), "envelope artifacts")
    missing = [key for key in ARTIFACT_KEYS if key not in raw_artifacts]
    if missing:
        raise _ContractError(f"envelope is missing artifacts: {', '.join(missing)}")
    artifacts: dict[str, Path] = {}
    digests: dict[str, str] = {}
    for key in ARTIFACT_KEYS:
        path, digest, _ = _digest_metadata(configured_root, raw_artifacts[key], f"artifact {key}")
        artifacts[key] = path
        digests[key] = digest
    if len(set(artifacts.values())) != len(artifacts):
        raise _ContractError("multiple artifact roles reference the same file")

    manifest = _load_json(artifacts["evidence_manifest"].read_bytes(), "evidence manifest")
    evidence_counts, successful = _validate_manifest(
        manifest, configured_root, date, run_id
    )
    quote_baseline = _manifest_quote_baseline(manifest)
    quote_evidence = (
        _validate_quote_baseline(quote_baseline, configured_root)
        if quote_baseline is not None
        else set()
    )
    capture_evidence = {path for path, _ in successful.values()}
    if set(artifacts.values()) & (capture_evidence | quote_evidence):
        raise _ContractError("an evidence capture overlaps a derived artifact")
    if capture_evidence & quote_evidence:
        raise _ContractError("market quote evidence overlaps source capture evidence")

    canonical = _load_json(artifacts["canonical_dataset"].read_bytes(), "canonical dataset")
    identities, source_counts, market_counts = _validate_dataset(
        canonical,
        configured_root,
        date,
        run_id,
        digests["evidence_manifest"],
        evidence_counts,
        successful,
    )
    canonical_baseline = _document_quote_baseline(canonical, "canonical dataset")
    if canonical_baseline != quote_baseline:
        raise _ContractError(
            "canonical market quote baseline differs from the evidence manifest"
        )
    report = _load_json(artifacts["report_json"].read_bytes(), "report JSON")
    _validate_report(
        report,
        date,
        run_id,
        digests["canonical_dataset"],
        identities,
        source_counts,
        market_counts,
        evidence_counts,
    )
    _validate_calendar_layers(envelope, manifest, canonical, report)
    report_baseline = _document_quote_baseline(report, "report JSON")
    if report_baseline != quote_baseline:
        raise _ContractError(
            "report market quote baseline differs from the canonical dataset"
        )
    envelope_baseline = _document_quote_baseline(envelope, "envelope")
    if envelope_baseline != quote_baseline:
        raise _ContractError(
            "envelope market quote baseline differs from the canonical dataset"
        )
    _validate_html(artifacts["report_html"], digests["report_json"])

    counts_raw = _mapping(envelope.get("counts"), "envelope counts")
    counts = {key: _plain_int(counts_raw.get(key), f"envelope counts.{key}") for key in COUNT_KEYS}
    if {key: counts[key] for key in EVIDENCE_COUNT_KEYS} != evidence_counts:
        raise _ContractError("envelope evidence counts do not match the manifest")
    if counts["expected"] != counts["succeeded"] + counts["failed"] + counts["pruned"]:
        raise _ContractError("envelope terminal source counts do not add up")
    if counts["canonical_records"] != len(identities):
        raise _ContractError("envelope canonical_records does not match the dataset")
    if counts["sources"] != len(source_counts) or counts["markets"] != len(market_counts):
        raise _ContractError("envelope aggregate cardinalities do not match the dataset")
    if _count_mapping(envelope.get("source_counts"), "envelope source_counts") != source_counts:
        raise _ContractError("envelope source_counts do not match the dataset")
    if _count_mapping(envelope.get("market_counts"), "envelope market_counts") != market_counts:
        raise _ContractError("envelope market_counts do not match the dataset")
    _lineage(envelope, "report_json", "canonical_dataset", digests["canonical_dataset"])
    _lineage(envelope, "report_html", "report_json", digests["report_json"])

    return IntakeResult(
        ok=True,
        workspace=configured_root,
        date=date,
        artifacts=artifacts,
        counts=counts,
        envelope=envelope,
    )


def _failure_message(envelope: Mapping[str, Any]) -> str:
    """Format a machine failure envelope without accepting its artifacts."""

    quality = envelope.get("quality")
    if isinstance(quality, dict):
        issues = quality.get("issues")
        if isinstance(issues, list):
            clean = [item.strip() for item in issues if isinstance(item, str) and item.strip()]
            if clean:
                return "daily intake quality gate rejected the session: " + "; ".join(clean)
    error = envelope.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return "daily intake failed: " + message.strip()
    return "daily intake did not produce an accepted market session"


def _validate_rejected(
    envelope: dict[str, Any], configured_root: Path, requested_date: str | None
) -> IntakeResult:
    """Expose verified evidence diagnostics while keeping reports unopenable."""

    _version(envelope, INTAKE_SCHEMA, "intake envelope")
    if envelope.get("ok") is not False:
        raise _ContractError("rejected intake envelope must declare ok=false")
    date_value = envelope.get("date")
    date = _real_date(date_value, "envelope date") if date_value is not None else None
    if requested_date is not None and date is not None and date != requested_date:
        raise _ContractError("envelope date does not match the requested date")
    root_value = envelope.get("workspace_root")
    if root_value is not None:
        root = Path(_string(root_value, "envelope workspace_root")).expanduser().resolve(
            strict=True
        )
        if not root.is_dir() or root != configured_root:
            raise _ContractError(
                "envelope workspace_root does not match the configured workspace"
            )
    artifacts: dict[str, Path] = {}
    raw_artifacts = envelope.get("artifacts")
    if isinstance(raw_artifacts, dict):
        forbidden = {"report_json", "report_html"} & set(raw_artifacts)
        if forbidden:
            raise _ContractError(
                "rejected intake must not expose report artifacts: "
                + ", ".join(sorted(forbidden))
            )
        for key in REJECTED_ARTIFACT_KEYS:
            if key in raw_artifacts:
                path, _, _ = _digest_metadata(
                    configured_root, raw_artifacts[key], f"artifact {key}"
                )
                artifacts[key] = path
    envelope_baseline = _document_quote_baseline(envelope, "envelope")
    manifest: dict[str, Any] | None = None
    canonical: dict[str, Any] | None = None
    if artifacts:
        manifest_baseline: dict[str, Any] | None = None
        if "evidence_manifest" in artifacts:
            manifest = _load_json(
                artifacts["evidence_manifest"].read_bytes(), "evidence manifest"
            )
            manifest_baseline = _manifest_quote_baseline(manifest)
            quote_evidence = (
                _validate_quote_baseline(manifest_baseline, configured_root)
                if manifest_baseline is not None
                else set()
            )
            if set(artifacts.values()) & quote_evidence:
                raise _ContractError(
                    "market quote evidence overlaps a derived artifact"
                )
        if envelope_baseline != manifest_baseline:
            raise _ContractError(
                "rejected envelope market quote baseline differs from the evidence manifest"
            )
        if "canonical_dataset" in artifacts:
            canonical = _load_json(
                artifacts["canonical_dataset"].read_bytes(), "canonical dataset"
            )
            canonical_baseline = _document_quote_baseline(
                canonical, "canonical dataset"
            )
            if canonical_baseline != manifest_baseline:
                raise _ContractError(
                    "rejected canonical market quote baseline differs from the evidence manifest"
                )
        calendar_claimed = any(
            key in envelope for key in ("session", "market_sessions")
        ) or (manifest is not None and any(
            key in manifest for key in ("session", "market_sessions")
        )) or (canonical is not None and any(
            key in canonical for key in ("session", "market_sessions")
        ))
        if calendar_claimed:
            if manifest is None or canonical is None:
                raise _ContractError(
                    "rejected intake calendar metadata lacks diagnostic artifacts"
                )
            _validate_calendar_layers(envelope, manifest, canonical, None)
    elif envelope_baseline is not None:
        raise _ContractError(
            "rejected envelope cannot expose an unverified market quote baseline"
        )
    elif any(key in envelope for key in ("session", "market_sessions")):
        raise _ContractError(
            "rejected envelope cannot expose unverified calendar metadata"
        )
    counts: dict[str, int] = {}
    raw_counts = envelope.get("counts")
    if isinstance(raw_counts, dict):
        for key in COUNT_KEYS:
            if key in raw_counts:
                counts[key] = _plain_int(raw_counts[key], f"envelope counts.{key}")
    return IntakeResult(
        ok=False,
        workspace=configured_root,
        date=date,
        artifacts=artifacts,
        counts=counts,
        envelope=envelope,
        error=_failure_message(envelope),
        returncode=1,
    )


class IntakeDriver:
    """Launch and verify one device-neutral daily intake command."""

    def __init__(
        self,
        argv: Sequence[str],
        workspace_root: str | Path,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
            raise ValueError("argv must be a non-empty sequence of argument strings")
        command = tuple(argv)
        if any(not isinstance(item, str) or not item or "\0" in item for item in command):
            raise ValueError("argv must contain only non-empty argument strings")
        for item in command:
            option = item.split("=", 1)[0]
            if option in _RESERVED_OPTIONS:
                raise ValueError(f"argv must not set driver-owned option {option}")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < float(timeout_seconds) <= MAX_TIMEOUT_SECONDS
        ):
            raise ValueError(f"timeout_seconds must be in (0, {MAX_TIMEOUT_SECONDS:g}]")
        root = Path(workspace_root).expanduser().resolve()
        if root == Path(root.anchor):
            raise ValueError("workspace_root cannot be a filesystem root")
        self.argv = command
        self.workspace_root = root
        self.timeout_seconds = float(timeout_seconds)

    def run(self, date: str | None = None) -> IntakeResult:
        """Run intake and return only a fully verified workspace as success."""
        requested_date: str | None = None
        if date is not None:
            try:
                requested_date = _real_date(date, "requested date")
            except _ContractError as exc:
                return self._failure(str(exc), returncode=3)
        command = [*self.argv, "--workspace-root", str(self.workspace_root), "--json"]
        if requested_date is not None:
            command.extend(("--date", requested_date))
        out = run_bounded(
            command,
            self.timeout_seconds,
            label="daily intake command",
            text=False,
            # The capture runs for minutes; without a parent-death watch a
            # TUI that dies mid-capture leaves the product writing the
            # workspace with nobody to verify or stop it.
            parent_death=True,
        )
        if not out["ok"]:
            if "not found" in out["error"]:
                return self._failure(out["error"], returncode=127)
            if "timed out" in out["error"]:
                return self._failure(out["error"], returncode=124)
            return self._failure(out["error"], returncode=126)

        stderr = out["stderr"].decode("utf-8", errors="replace")
        if out["returncode"] != 0:
            try:
                envelope = _load_json(out["stdout"] or b"", "daily intake stdout")
                rejected = _validate_rejected(
                    envelope, self.workspace_root, requested_date
                )
            except (OSError, _ContractError):
                rejected = None
            if rejected is not None:
                return IntakeResult(
                    ok=False,
                    workspace=rejected.workspace,
                    date=rejected.date,
                    artifacts=rejected.artifacts,
                    counts=rejected.counts,
                    envelope=rejected.envelope,
                    error=rejected.error,
                    returncode=out['returncode'],
                    stderr=stderr,
                )
            detail = stderr.strip()[:500]
            message = f"daily intake exited with status {out['returncode']}"
            if detail:
                message += f": {detail}"
            return self._failure(
                message,
                returncode=out['returncode'],
                stderr=stderr,
            )
        try:
            envelope = _load_json(out['stdout'] or b"", "daily intake stdout")
            result = _validate_success(envelope, self.workspace_root, requested_date)
        except (OSError, _ContractError) as exc:
            return self._failure(
                f"daily intake contract rejected: {exc}",
                returncode=2,
                stderr=stderr,
            )
        return IntakeResult(
            ok=True,
            workspace=result.workspace,
            date=result.date,
            artifacts=result.artifacts,
            counts=result.counts,
            envelope=result.envelope,
            returncode=out['returncode'],
            stderr=stderr,
        )

    def _failure(
        self,
        message: str,
        *,
        returncode: int,
        stderr: str = "",
    ) -> IntakeResult:
        return IntakeResult(
            ok=False,
            workspace=self.workspace_root,
            error=message,
            returncode=returncode,
            stderr=stderr,
        )


__all__ = [
    "ARTIFACT_KEYS",
    "CONTRACT_REVISION",
    "DATASET_SCHEMA",
    "DEFAULT_TIMEOUT_SECONDS",
    "EVIDENCE_SCHEMA",
    "INTAKE_SCHEMA",
    "IntakeDriver",
    "IntakeResult",
    "MAX_TIMEOUT_SECONDS",
    "REPORT_SCHEMA",
]
