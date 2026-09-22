"""Strict offline loader for GALAHAD OHLCV consensus manifests.

The configured directory is treated as an untrusted evidence store.  This
module performs no network access and has no provider or cache fallback.  A
caller receives bars only after the manifest identity, source independence,
candle geometry, and canonical hashes have all been verified.

Canonical JSON means UTF-8 encoded JSON with object keys sorted, no optional
whitespace, non-ASCII text preserved, and non-finite numbers rejected.  Each
contract hash also includes the producer's ASCII domain plus one NUL byte.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


MANIFEST_SCHEMA = "quantkit.ohlcv-consensus.v1"
DEFAULT_MAX_FILES = 512
DEFAULT_MAX_FILE_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_BARS = 250_000
DEFAULT_MAX_ENTRIES = 4_096
DEFAULT_MAX_SOURCES = 128
DEFAULT_MAX_REASON_CODES = 128

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_FRAME_HASH_DOMAIN = "quantkit.ohlcv-source-frame.v1"
_CALENDAR_HASH_DOMAIN = "quantkit.ohlcv-session-calendar.v1"
_BARS_HASH_DOMAIN = "quantkit.ohlcv-accepted-bars.v1"
_OUTPUT_HASH_DOMAIN = "quantkit.ohlcv-consensus-manifest.v1"
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "status",
        "identity",
        "policy",
        "calendar",
        "bars",
        "bars_sha256",
        "sources",
        "diagnostics",
        "output_sha256",
    }
)
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
_BAR_FIELDS = frozenset({"date", "open", "high", "low", "close", "volume"})
_SOURCE_FIELDS = frozenset(
    {
        "source_id",
        "provider",
        "independence_group",
        "price_basis",
        "volume_unit",
        "identity",
        "input_artifact_sha256",
        "frame_sha256",
        "status",
        "reason_codes",
    }
)
_SOURCE_IDENTITY_FIELDS = frozenset({"symbol", "market", "currency"})
_STATUSES = frozenset({"accepted", "partial", "quarantined"})
_SOURCE_STATUSES = frozenset({"ready", "partial", "invalid", "empty"})
_POLICY_FIELDS = frozenset(
    {
        "price_relative",
        "volume_relative",
        "minimum_independent_groups",
        "maximum_independent_groups",
        "alignment",
        "vote_rule",
        "hash_domains",
    }
)
_TOLERANCE_FIELDS = frozenset({"pass", "warning", "quarantine"})
_HASH_DOMAIN_FIELDS = frozenset(
    {
        "frame_sha256",
        "calendar_sessions_sha256",
        "bars_sha256",
        "output_sha256",
    }
)
_CALENDAR_FIELDS = frozenset({"name", "sessions", "sessions_sha256"})
_DIAGNOSTIC_FIELDS = frozenset(
    {"date", "status", "reason_codes", "supporting_groups", "sources", "fields"}
)
_DIAGNOSTIC_STATUSES = frozenset({"accepted", "warning", "quarantined"})
_DIAGNOSTIC_SOURCE_FIELDS = frozenset(
    {"source_id", "provider", "independence_group", "status", "reason_codes"}
)
_DIAGNOSTIC_SOURCE_STATUSES = frozenset(
    {"contributed", "excluded", "missing", "invalid"}
)
_DIAGNOSTIC_VALUE_FIELDS = frozenset(
    {
        "status",
        "consensus",
        "relative_spread",
        "contributing_sources",
        "excluded_sources",
    }
)
_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


class ValidatedBarsError(ValueError):
    """Base class for store, manifest, and lookup contract failures."""


class StoreConfigurationError(ValidatedBarsError):
    """The configured store cannot be traversed safely."""


class ManifestValidationError(ValidatedBarsError):
    """A candidate manifest violates the consensus contract."""


class ManifestLookupError(ValidatedBarsError):
    """A requested identity is missing, ambiguous, or not accepted."""


class _DuplicateKeyError(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number: {value}")
    return parsed


def canonical_json_bytes(value: Any) -> bytes:
    """Return the contract's deterministic canonical JSON representation."""

    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ManifestValidationError(f"value is not canonical JSON: {exc}") from exc
    return encoded.encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    """Return a lowercase SHA-256 digest of canonical JSON bytes."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _domain_digest(domain: str, value: Any) -> str:
    payload = domain.encode("ascii") + b"\0" + canonical_json_bytes(value)
    return hashlib.sha256(payload).hexdigest()


def _load_json(payload: bytes, label: str) -> dict[str, Any]:
    try:
        text = payload.decode("utf-8")
        document = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateKeyError,
        ValueError,
        RecursionError,
    ) as exc:
        raise ManifestValidationError(f"{label} is not strict JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ManifestValidationError(f"{label} root must be an object")
    return document


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    observed = frozenset(value)
    if observed == expected:
        return
    missing = sorted(expected - observed)
    unknown = sorted(observed - expected)
    detail: list[str] = []
    if missing:
        detail.append("missing " + ", ".join(missing))
    if unknown:
        detail.append("unknown " + ", ".join(unknown))
    raise ManifestValidationError(f"{label} has invalid fields ({'; '.join(detail)})")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestValidationError(f"{label} must be an object")
    return value


def _plain_string(value: Any, label: str, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ManifestValidationError(f"{label} must be a bounded plain string")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ManifestValidationError(
            f"{label} must be 64 lowercase hexadecimal characters"
        )
    return value


def _number(value: Any, label: str, *, positive: bool) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ManifestValidationError(f"{label} must be a JSON number")
    if not math.isfinite(value):
        raise ManifestValidationError(f"{label} must be finite")
    if positive and value <= 0:
        raise ManifestValidationError(f"{label} must be positive")
    if not positive and value < 0:
        raise ManifestValidationError(f"{label} must be non-negative")
    return value


def _finite_nonnegative(value: Any, label: str) -> int | float:
    return _number(value, label, positive=False)


def _policy_float(value: Any, label: str) -> float:
    if not isinstance(value, float) or not math.isfinite(value) or value < 0:
        raise ManifestValidationError(f"{label} must be a finite non-negative float")
    return value


def _reason_codes(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ManifestValidationError(f"{label} must be an array")
    if len(value) > DEFAULT_MAX_REASON_CODES:
        raise ManifestValidationError(
            f"{label} exceeds the {DEFAULT_MAX_REASON_CODES} entry limit"
        )
    result = tuple(
        _plain_string(item, f"{label}[{position}]", maximum=128)
        for position, item in enumerate(value)
    )
    if list(result) != sorted(set(result)):
        raise ManifestValidationError(f"{label} must be sorted and unique")
    return result


def _string_list(
    value: Any,
    label: str,
    *,
    maximum_items: int = DEFAULT_MAX_SOURCES,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ManifestValidationError(f"{label} must be an array")
    if len(value) > maximum_items:
        raise ManifestValidationError(
            f"{label} exceeds the {maximum_items} entry limit"
        )
    result = tuple(
        _plain_string(item, f"{label}[{position}]")
        for position, item in enumerate(value)
    )
    if list(result) != sorted(set(result)):
        raise ManifestValidationError(f"{label} must be sorted and unique")
    return result


def _calendar_date(value: Any, label: str) -> str:
    raw = _plain_string(value, label, maximum=10)
    if len(raw) != 10:
        raise ManifestValidationError(f"{label} must use YYYY-MM-DD")
    try:
        parsed = dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise ManifestValidationError(f"{label} must be a real YYYY-MM-DD date") from exc
    if parsed.isoformat() != raw:
        raise ManifestValidationError(f"{label} must use canonical YYYY-MM-DD")
    return raw


def _validate_identity(value: Any, label: str) -> dict[str, str]:
    identity = _object(value, label)
    _exact_fields(identity, _IDENTITY_FIELDS, label)
    parsed = {
        key: _plain_string(identity.get(key), f"{label}.{key}")
        for key in _IDENTITY_FIELDS
    }
    if parsed["interval"] != "1d":
        raise ManifestValidationError(f"{label}.interval must be 1d")
    # The canonical form of a symbol is uppercase; downstream checks
    # (forecast request binding, consensus link, chart server) all compare
    # against this form. Rejecting non-canonical identities here keeps every
    # later comparison on one anchor instead of silently deadlocking on a
    # lowercase manifest symbol.
    if parsed["symbol"] != parsed["symbol"].upper():
        raise ManifestValidationError(
            f"{label}.symbol must be canonical uppercase"
        )
    return parsed


def _validate_tolerance(value: Any, label: str) -> dict[str, int | float]:
    tolerance = _object(value, label)
    _exact_fields(tolerance, _TOLERANCE_FIELDS, label)
    passed = _policy_float(tolerance.get("pass"), f"{label}.pass")
    warning = _policy_float(tolerance.get("warning"), f"{label}.warning")
    quarantine = _policy_float(
        tolerance.get("quarantine"), f"{label}.quarantine"
    )
    if not passed <= warning < quarantine:
        raise ManifestValidationError(
            f"{label} must satisfy 0 <= pass <= warning < quarantine"
        )
    return {"pass": passed, "warning": warning, "quarantine": quarantine}


def _validate_policy(value: Any, label: str) -> dict[str, Any]:
    policy = _object(value, label)
    _exact_fields(policy, _POLICY_FIELDS, label)
    _validate_tolerance(policy.get("price_relative"), f"{label}.price_relative")
    _validate_tolerance(policy.get("volume_relative"), f"{label}.volume_relative")
    if type(policy.get("minimum_independent_groups")) is not int or policy.get(
        "minimum_independent_groups"
    ) != 2:
        raise ManifestValidationError(
            f"{label}.minimum_independent_groups must be 2"
        )
    if type(policy.get("maximum_independent_groups")) is not int or policy.get(
        "maximum_independent_groups"
    ) != 3:
        raise ManifestValidationError(
            f"{label}.maximum_independent_groups must be 3"
        )
    if policy.get("alignment") != "observed_union_no_forward_fill":
        raise ManifestValidationError(f"{label}.alignment is unsupported")
    if policy.get("vote_rule") != "hierarchical_independence_group_2_of_3_median":
        raise ManifestValidationError(f"{label}.vote_rule is unsupported")
    hash_domains = _object(policy.get("hash_domains"), f"{label}.hash_domains")
    _exact_fields(hash_domains, _HASH_DOMAIN_FIELDS, f"{label}.hash_domains")
    expected_domains = {
        "frame_sha256": _FRAME_HASH_DOMAIN,
        "calendar_sessions_sha256": _CALENDAR_HASH_DOMAIN,
        "bars_sha256": _BARS_HASH_DOMAIN,
        "output_sha256": _OUTPUT_HASH_DOMAIN,
    }
    if hash_domains != expected_domains:
        raise ManifestValidationError(f"{label}.hash_domains are unsupported")
    return policy


def _validate_calendar(
    value: Any, identity: Mapping[str, str], label: str
) -> tuple[str, ...]:
    calendar = _object(value, label)
    _exact_fields(calendar, _CALENDAR_FIELDS, label)
    name = _plain_string(calendar.get("name"), f"{label}.name")
    if name != identity["calendar"]:
        raise ManifestValidationError(f"{label}.name differs from identity.calendar")
    raw_sessions = calendar.get("sessions")
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise ManifestValidationError(f"{label}.sessions must be a non-empty array")
    if len(raw_sessions) > DEFAULT_MAX_BARS:
        raise ManifestValidationError(
            f"{label}.sessions exceeds the {DEFAULT_MAX_BARS} entry limit"
        )
    sessions = tuple(
        _calendar_date(item, f"{label}.sessions[{position}]")
        for position, item in enumerate(raw_sessions)
    )
    if list(sessions) != sorted(set(sessions)):
        raise ManifestValidationError(
            f"{label}.sessions must be unique and strictly ascending"
        )
    expected_digest = _digest(
        calendar.get("sessions_sha256"), f"{label}.sessions_sha256"
    )
    if _domain_digest(_CALENDAR_HASH_DOMAIN, raw_sessions) != expected_digest:
        raise ManifestValidationError(
            f"{label}.sessions_sha256 does not match sessions"
        )
    return sessions


def _validate_bars(value: Any, label: str, max_bars: int) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ManifestValidationError(f"{label} must be an array")
    if len(value) > max_bars:
        raise ManifestValidationError(f"{label} exceeds the {max_bars} record limit")

    result: list[dict[str, Any]] = []
    previous_date: str | None = None
    for position, raw_record in enumerate(value):
        record_label = f"{label}[{position}]"
        record = _object(raw_record, record_label)
        _exact_fields(record, _BAR_FIELDS, record_label)
        date = _calendar_date(record.get("date"), f"{record_label}.date")
        if previous_date is not None and date <= previous_date:
            raise ManifestValidationError(
                f"{label} dates must be unique and strictly ascending"
            )
        previous_date = date

        opened = _number(record.get("open"), f"{record_label}.open", positive=True)
        high = _number(record.get("high"), f"{record_label}.high", positive=True)
        low = _number(record.get("low"), f"{record_label}.low", positive=True)
        close = _number(record.get("close"), f"{record_label}.close", positive=True)
        volume = _number(
            record.get("volume"), f"{record_label}.volume", positive=False
        )
        if high < max(opened, low, close) or low > min(opened, high, close):
            raise ManifestValidationError(f"{record_label} has invalid candle geometry")
        result.append(
            {
                "date": date,
                "open": opened,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
    return tuple(result)


def _validate_sources(
    value: Any, identity: Mapping[str, str], label: str
) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or len(value) < 2:
        raise ManifestValidationError(f"{label} must contain at least two sources")
    if len(value) > DEFAULT_MAX_SOURCES:
        raise ManifestValidationError(
            f"{label} exceeds the {DEFAULT_MAX_SOURCES} source limit"
        )

    result: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    independence_groups: set[str] = set()
    provider_groups: dict[str, str] = {}
    for position, raw_source in enumerate(value):
        source_label = f"{label}[{position}]"
        source = _object(raw_source, source_label)
        _exact_fields(source, _SOURCE_FIELDS, source_label)
        parsed = {
            "source_id": _plain_string(
                source.get("source_id"), f"{source_label}.source_id"
            ),
            "provider": _plain_string(source.get("provider"), f"{source_label}.provider"),
            "independence_group": _plain_string(
                source.get("independence_group"),
                f"{source_label}.independence_group",
            ),
            "price_basis": _plain_string(
                source.get("price_basis"), f"{source_label}.price_basis"
            ),
            "volume_unit": _plain_string(
                source.get("volume_unit"), f"{source_label}.volume_unit"
            ),
            "identity": _validate_source_identity(
                source.get("identity"), identity, f"{source_label}.identity"
            ),
            "input_artifact_sha256": _digest(
                source.get("input_artifact_sha256"),
                f"{source_label}.input_artifact_sha256",
            ),
            "frame_sha256": _digest(
                source.get("frame_sha256"), f"{source_label}.frame_sha256"
            ),
            "status": source.get("status"),
            "reason_codes": list(
                _reason_codes(source.get("reason_codes"), f"{source_label}.reason_codes")
            ),
        }
        if parsed["status"] not in _SOURCE_STATUSES:
            raise ManifestValidationError(f"{source_label}.status is invalid")
        if parsed["price_basis"] != identity["price_basis"]:
            raise ManifestValidationError(
                f"{source_label}.price_basis differs from the manifest identity"
            )
        if parsed["volume_unit"] != identity["volume_unit"]:
            raise ManifestValidationError(
                f"{source_label}.volume_unit differs from the manifest identity"
            )
        source_id = parsed["source_id"]
        if source_id in source_ids:
            raise ManifestValidationError(f"{label} contains a duplicate source_id")
        source_ids.add(source_id)
        provider = parsed["provider"]
        group = parsed["independence_group"]
        previous_group = provider_groups.setdefault(provider, group)
        if previous_group != group:
            raise ManifestValidationError(
                f"{label} assigns one provider to multiple independence groups"
            )
        independence_groups.add(parsed["independence_group"])
        result.append(parsed)
    if len(independence_groups) < 2:
        raise ManifestValidationError(
            f"{label} must contain at least two independent groups"
        )
    if len(independence_groups) > 3:
        raise ManifestValidationError(f"{label} may contain at most three groups")
    if [item["source_id"] for item in result] != sorted(source_ids):
        raise ManifestValidationError(f"{label} must be sorted by source_id")
    return tuple(result)


def _validate_source_identity(
    value: Any, identity: Mapping[str, str], label: str
) -> dict[str, str]:
    source_identity = _object(value, label)
    _exact_fields(source_identity, _SOURCE_IDENTITY_FIELDS, label)
    parsed = {
        key: _plain_string(source_identity.get(key), f"{label}.{key}")
        for key in _SOURCE_IDENTITY_FIELDS
    }
    expected = {key: identity[key] for key in _SOURCE_IDENTITY_FIELDS}
    if parsed != expected:
        raise ManifestValidationError(
            f"{label} differs from the manifest security identity"
        )
    return parsed


def _positive_limit(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StoreConfigurationError(f"{label} must be a positive integer")
    return value


def _validate_diagnostic_sources(
    value: Any,
    sources: Mapping[str, Mapping[str, Any]],
    label: str,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or len(value) != len(sources):
        raise ManifestValidationError(
            f"{label} must contain exactly one decision per source"
        )
    result: list[dict[str, Any]] = []
    observed_ids: list[str] = []
    for position, raw_item in enumerate(value):
        item_label = f"{label}[{position}]"
        item = _object(raw_item, item_label)
        _exact_fields(item, _DIAGNOSTIC_SOURCE_FIELDS, item_label)
        source_id = _plain_string(item.get("source_id"), f"{item_label}.source_id")
        expected = sources.get(source_id)
        if expected is None:
            raise ManifestValidationError(f"{item_label}.source_id is unknown")
        if item.get("provider") != expected["provider"]:
            raise ManifestValidationError(f"{item_label}.provider differs from source")
        if item.get("independence_group") != expected["independence_group"]:
            raise ManifestValidationError(
                f"{item_label}.independence_group differs from source"
            )
        decision_status = item.get("status")
        if decision_status not in _DIAGNOSTIC_SOURCE_STATUSES:
            raise ManifestValidationError(f"{item_label}.status is invalid")
        reasons = _reason_codes(
            item.get("reason_codes"), f"{item_label}.reason_codes"
        )
        if decision_status == "contributed" and reasons:
            raise ManifestValidationError(
                f"{item_label} contributed source cannot have reason codes"
            )
        if decision_status != "contributed" and not reasons:
            raise ManifestValidationError(
                f"{item_label} non-contributing source needs a reason code"
            )
        observed_ids.append(source_id)
        result.append(item)
    if observed_ids != sorted(sources):
        raise ManifestValidationError(f"{label} must be sorted by source_id")
    return tuple(result)


def _validate_diagnostic_fields(
    value: Any,
    source_ids: set[str],
    policy: Mapping[str, Any],
    label: str,
) -> dict[str, dict[str, Any]]:
    fields = _object(value, label)
    _exact_fields(fields, frozenset(_OHLCV_COLUMNS), label)
    result: dict[str, dict[str, Any]] = {}
    for column in _OHLCV_COLUMNS:
        item_label = f"{label}.{column}"
        item = _object(fields.get(column), item_label)
        _exact_fields(item, _DIAGNOSTIC_VALUE_FIELDS, item_label)
        status = item.get("status")
        if status not in _DIAGNOSTIC_STATUSES:
            raise ManifestValidationError(f"{item_label}.status is invalid")
        consensus = item.get("consensus")
        spread = item.get("relative_spread")
        if status == "quarantined":
            if consensus is not None or spread is not None:
                raise ManifestValidationError(
                    f"{item_label} quarantined values must be null"
                )
        else:
            _number(
                consensus,
                f"{item_label}.consensus",
                positive=(column != "volume"),
            )
            parsed_spread = _finite_nonnegative(
                spread, f"{item_label}.relative_spread"
            )
            tolerance_name = "volume_relative" if column == "volume" else "price_relative"
            tolerance = policy[tolerance_name]
            if parsed_spread > tolerance["warning"]:
                raise ManifestValidationError(
                    f"{item_label}.relative_spread exceeds the publishable tolerance"
                )
            expected_status = (
                "accepted" if parsed_spread <= tolerance["pass"] else "warning"
            )
            if status != expected_status:
                raise ManifestValidationError(
                    f"{item_label}.status conflicts with relative_spread"
                )
        contributing = _string_list(
            item.get("contributing_sources"), f"{item_label}.contributing_sources"
        )
        excluded = _string_list(
            item.get("excluded_sources"), f"{item_label}.excluded_sources"
        )
        if set(contributing) & set(excluded):
            raise ManifestValidationError(
                f"{item_label} source sets must not overlap"
            )
        if set(contributing) | set(excluded) != source_ids:
            raise ManifestValidationError(
                f"{item_label} source sets must partition all sources"
            )
        result[column] = item
    return result


def _validate_diagnostics(
    value: Any,
    sources: tuple[dict[str, Any], ...],
    calendar_sessions: tuple[str, ...],
    records: tuple[dict[str, Any], ...],
    policy: Mapping[str, Any],
    label: str,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        raise ManifestValidationError(f"{label} must be an array")
    if len(value) > DEFAULT_MAX_BARS:
        raise ManifestValidationError(
            f"{label} exceeds the {DEFAULT_MAX_BARS} entry limit"
        )
    source_map = {item["source_id"]: item for item in sources}
    result: list[dict[str, Any]] = []
    diagnostic_dates: list[str] = []
    accepted_by_date = {record["date"]: record for record in records}
    for position, raw_item in enumerate(value):
        item_label = f"{label}[{position}]"
        item = _object(raw_item, item_label)
        _exact_fields(item, _DIAGNOSTIC_FIELDS, item_label)
        date = _calendar_date(item.get("date"), f"{item_label}.date")
        status = item.get("status")
        if status not in _DIAGNOSTIC_STATUSES:
            raise ManifestValidationError(f"{item_label}.status is invalid")
        reasons = _reason_codes(
            item.get("reason_codes"), f"{item_label}.reason_codes"
        )
        supporting = _string_list(
            item.get("supporting_groups"), f"{item_label}.supporting_groups", maximum_items=3
        )
        source_decisions = _validate_diagnostic_sources(
            item.get("sources"), source_map, f"{item_label}.sources"
        )
        fields = _validate_diagnostic_fields(
            item.get("fields"), set(source_map), policy, f"{item_label}.fields"
        )
        known_groups = {source["independence_group"] for source in sources}
        if not set(supporting) <= known_groups:
            raise ManifestValidationError(
                f"{item_label}.supporting_groups contains an unknown group"
            )
        contributed = {
            decision["source_id"]
            for decision in source_decisions
            if decision["status"] == "contributed"
        }
        contributing_groups = {
            source_map[source_id]["independence_group"]
            for source_id in contributed
        }
        if contributing_groups != set(supporting):
            raise ManifestValidationError(
                f"{item_label}.supporting_groups do not match contributing sources"
            )
        for column in _OHLCV_COLUMNS:
            field_contributors = set(fields[column]["contributing_sources"])
            if field_contributors != contributed:
                raise ManifestValidationError(
                    f"{item_label}.fields.{column}.contributing_sources "
                    "differ from source decisions"
                )

        if status == "quarantined":
            if not reasons:
                raise ManifestValidationError(
                    f"{item_label} quarantined session needs a reason code"
                )
            if date in accepted_by_date or supporting:
                raise ManifestValidationError(
                    f"{item_label} quarantined session cannot publish a bar"
                )
            if contributed:
                raise ManifestValidationError(
                    f"{item_label} quarantined session cannot have contributors"
                )
            if any(fields[column]["status"] != "quarantined" for column in _OHLCV_COLUMNS):
                raise ManifestValidationError(
                    f"{item_label} quarantined session needs quarantined fields"
                )
        else:
            record = accepted_by_date.get(date)
            if record is None or date not in calendar_sessions:
                raise ManifestValidationError(
                    f"{item_label} accepted session must bind a calendar bar"
                )
            if len(supporting) < 2:
                raise ManifestValidationError(
                    f"{item_label} accepted session needs two supporting groups"
                )
            for column in _OHLCV_COLUMNS:
                if fields[column]["consensus"] != record[column]:
                    raise ManifestValidationError(
                        f"{item_label}.fields.{column}.consensus differs from bars"
                    )
            if len(contributing_groups) < 2:
                raise ManifestValidationError(
                    f"{item_label} lacks two contributing independent groups"
                )
            if any(fields[column]["status"] == "quarantined" for column in _OHLCV_COLUMNS):
                raise ManifestValidationError(
                    f"{item_label} published session cannot have quarantined fields"
                )
            if status == "accepted":
                if reasons:
                    raise ManifestValidationError(
                        f"{item_label} accepted session cannot have reason codes"
                    )
                if any(
                    fields[column]["status"] != "accepted"
                    for column in _OHLCV_COLUMNS
                ):
                    raise ManifestValidationError(
                        f"{item_label} accepted session needs accepted fields"
                    )
                if len(contributed) != len(source_map):
                    raise ManifestValidationError(
                        f"{item_label} accepted session needs every source to contribute"
                    )
            elif not reasons:
                raise ManifestValidationError(
                    f"{item_label} warning session needs a reason code"
                )
            field_warning = any(
                fields[column]["status"] == "warning" for column in _OHLCV_COLUMNS
            )
            if ("field_tolerance_warning" in reasons) != field_warning:
                raise ManifestValidationError(
                    f"{item_label} field warning reason conflicts with field statuses"
                )
            incomplete_support = len(contributed) != len(source_map)
            if ("incomplete_source_support" in reasons) != incomplete_support:
                raise ManifestValidationError(
                    f"{item_label} incomplete-support reason conflicts with source decisions"
                )
        diagnostic_dates.append(date)
        result.append(item)
    if diagnostic_dates != sorted(set(diagnostic_dates)):
        raise ManifestValidationError(f"{label} dates must be unique and ascending")
    required_dates = set(calendar_sessions) | set(accepted_by_date)
    if required_dates - set(diagnostic_dates):
        raise ManifestValidationError(
            f"{label} does not cover every calendar session and accepted bar"
        )
    return tuple(result)


@dataclass(frozen=True)
class ValidatedBars:
    """A verified manifest and its immutable-by-convention OHLCV records."""

    records: tuple[dict[str, Any], ...]
    manifest: dict[str, Any]
    path: Path

    def to_frame(self):
        """Return a pandas OHLCV frame, importing pandas only when requested."""

        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover - depends on optional install
            raise RuntimeError("pandas is required for ValidatedBars.to_frame()") from exc
        if not self.records:
            frame = pd.DataFrame(columns=_OHLCV_COLUMNS, dtype=float)
            frame.index = pd.DatetimeIndex([], name="date")
            return frame
        frame = pd.DataFrame([dict(record) for record in self.records])
        frame["date"] = pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="raise")
        frame = frame.set_index("date")
        frame.index.name = "date"
        return frame[["open", "high", "low", "close", "volume"]]


def _validate_manifest(
    document: dict[str, Any], path: Path, max_bars: int
) -> ValidatedBars:
    label = str(path)
    _exact_fields(document, _TOP_LEVEL_FIELDS, label)
    if document.get("schema") != MANIFEST_SCHEMA:
        raise ManifestValidationError(
            f"{label} uses unsupported schema {document.get('schema')!r}"
        )
    status_value = document.get("status")
    if status_value not in _STATUSES:
        raise ManifestValidationError(f"{label}.status is invalid")

    identity = _validate_identity(document.get("identity"), f"{label}.identity")
    policy = _validate_policy(document.get("policy"), f"{label}.policy")
    calendar_sessions = _validate_calendar(
        document.get("calendar"), identity, f"{label}.calendar"
    )
    records = _validate_bars(document.get("bars"), f"{label}.bars", max_bars)
    if any(record["date"] not in calendar_sessions for record in records):
        raise ManifestValidationError(f"{label}.bars contains a non-session date")
    sources = _validate_sources(document.get("sources"), identity, f"{label}.sources")
    diagnostics = _validate_diagnostics(
        document.get("diagnostics"),
        sources,
        calendar_sessions,
        records,
        policy,
        f"{label}.diagnostics",
    )
    expected_status = (
        "quarantined"
        if not records
        else "partial"
        if any(item["status"] != "accepted" for item in diagnostics)
        else "accepted"
    )
    if status_value != expected_status:
        raise ManifestValidationError(f"{label}.status conflicts with its records")

    expected_bars_digest = _digest(document.get("bars_sha256"), f"{label}.bars_sha256")
    observed_bars_digest = _domain_digest(_BARS_HASH_DOMAIN, document["bars"])
    if observed_bars_digest != expected_bars_digest:
        raise ManifestValidationError(f"{label}.bars_sha256 does not match bars")

    expected_output_digest = _digest(
        document.get("output_sha256"), f"{label}.output_sha256"
    )
    output_payload = {
        key: value for key, value in document.items() if key != "output_sha256"
    }
    observed_output_digest = _domain_digest(_OUTPUT_HASH_DOMAIN, output_payload)
    if observed_output_digest != expected_output_digest:
        raise ManifestValidationError(
            f"{label}.output_sha256 does not match the manifest"
        )

    return ValidatedBars(
        records=tuple(copy.deepcopy(record) for record in records),
        manifest=copy.deepcopy(document),
        path=path,
    )


class ValidatedBarsStore:
    """Read and select consensus manifests from one bounded offline directory."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_files: int = DEFAULT_MAX_FILES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_bars: int = DEFAULT_MAX_BARS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        if not isinstance(root, (str, os.PathLike)) or not str(root).strip():
            raise StoreConfigurationError("manifest root must be a configured path")
        self.max_files = _positive_limit(max_files, "max_files")
        self.max_file_bytes = _positive_limit(max_file_bytes, "max_file_bytes")
        self.max_bars = _positive_limit(max_bars, "max_bars")
        self.max_entries = _positive_limit(max_entries, "max_entries")

        configured = Path(root).expanduser()
        try:
            root_lstat = configured.lstat()
        except OSError as exc:
            raise StoreConfigurationError(f"manifest root is unavailable: {configured}") from exc
        if stat.S_ISLNK(root_lstat.st_mode):
            raise StoreConfigurationError("manifest root must not be a symlink")
        if not stat.S_ISDIR(root_lstat.st_mode):
            raise StoreConfigurationError("manifest root must be a directory")
        try:
            self.root = configured.resolve(strict=True)
        except OSError as exc:  # pragma: no cover - lstat above normally catches this
            raise StoreConfigurationError(f"manifest root is unavailable: {configured}") from exc

    def _candidate_paths(self) -> list[Path]:
        candidates: list[Path] = []
        seen_spellings: set[str] = set()
        seen_inodes: set[tuple[int, int]] = set()
        entry_count = 0

        def walk(directory: Path) -> None:
            nonlocal entry_count
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
            except OSError as exc:
                raise StoreConfigurationError(
                    f"cannot inspect manifest directory: {directory}"
                ) from exc
            for entry in entries:
                entry_count += 1
                if entry_count > self.max_entries:
                    raise StoreConfigurationError(
                        f"manifest store exceeds the {self.max_entries} entry limit"
                    )
                path = Path(entry.path)
                try:
                    if entry.is_symlink():
                        raise StoreConfigurationError(
                            f"manifest store contains a symlink: {path}"
                        )
                    if entry.is_dir(follow_symlinks=False):
                        walk(path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        raise StoreConfigurationError(
                            f"manifest store contains a non-regular entry: {path}"
                        )
                    if path.suffix.casefold() != ".json":
                        continue
                    relative = path.relative_to(self.root)
                    spelling = relative.as_posix().casefold()
                    if spelling in seen_spellings:
                        raise StoreConfigurationError(
                            f"manifest store contains ambiguous paths: {relative}"
                        )
                    seen_spellings.add(spelling)
                    metadata = entry.stat(follow_symlinks=False)
                    inode = (metadata.st_dev, metadata.st_ino)
                    if inode in seen_inodes:
                        raise StoreConfigurationError(
                            f"manifest store contains a duplicate file alias: {relative}"
                        )
                    seen_inodes.add(inode)
                    candidates.append(path)
                    if len(candidates) > self.max_files:
                        raise StoreConfigurationError(
                            f"manifest store exceeds the {self.max_files} file limit"
                        )
                except OSError as exc:
                    raise StoreConfigurationError(
                        f"cannot inspect manifest entry: {path}"
                    ) from exc

        walk(self.root)
        return sorted(candidates, key=lambda path: path.relative_to(self.root).as_posix())

    def _read_candidate(self, path: Path) -> bytes:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise StoreConfigurationError(f"manifest path escapes the store: {path}") from exc
        if resolved != path:
            raise StoreConfigurationError(f"manifest path is ambiguous: {path}")

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise StoreConfigurationError(f"cannot open manifest file: {path}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise StoreConfigurationError(f"manifest is not a regular file: {path}")
            if metadata.st_size > self.max_file_bytes:
                raise StoreConfigurationError(
                    f"manifest exceeds the {self.max_file_bytes} byte limit: {path}"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                payload = handle.read(self.max_file_bytes + 1)
            if len(payload) > self.max_file_bytes:
                raise StoreConfigurationError(
                    f"manifest exceeds the {self.max_file_bytes} byte limit: {path}"
                )
            return payload
        finally:
            os.close(descriptor)

    def _load_all(self) -> list[ValidatedBars]:
        manifests: list[ValidatedBars] = []
        identities: dict[tuple[str, ...], Path] = {}
        for path in self._candidate_paths():
            document = _load_json(self._read_candidate(path), str(path))
            validated = _validate_manifest(document, path, self.max_bars)
            identity = validated.manifest["identity"]
            identity_key = tuple(identity[key] for key in sorted(_IDENTITY_FIELDS))
            previous = identities.get(identity_key)
            if previous is not None:
                raise ManifestLookupError(
                    "duplicate manifest identity: "
                    f"{previous.relative_to(self.root)} and {path.relative_to(self.root)}"
                )
            identities[identity_key] = path
            manifests.append(validated)
        return manifests

    def lookup(
        self,
        symbol: str,
        *,
        market: str | None = None,
        interval: str | None = None,
        accepted_only: bool = True,
    ) -> ValidatedBars:
        """Return one exact match; never infer an identity or use a fallback."""

        requested_symbol = _plain_string(symbol, "symbol")
        requested_market = (
            _plain_string(market, "market") if market is not None else None
        )
        requested_interval = (
            _plain_string(interval, "interval") if interval is not None else None
        )
        if not isinstance(accepted_only, bool):
            raise ManifestLookupError("accepted_only must be a boolean")

        candidates: list[ValidatedBars] = []
        for item in self._load_all():
            identity = item.manifest["identity"]
            if identity["symbol"] != requested_symbol:
                continue
            if requested_market is not None and identity["market"] != requested_market:
                continue
            if requested_interval is not None and identity["interval"] != requested_interval:
                continue
            candidates.append(item)

        if not candidates:
            detail = f"symbol={requested_symbol!r}"
            if requested_market is not None:
                detail += f", market={requested_market!r}"
            if requested_interval is not None:
                detail += f", interval={requested_interval!r}"
            raise ManifestLookupError(f"no consensus manifest matches {detail}")

        eligible = (
            [item for item in candidates if item.manifest["status"] == "accepted"]
            if accepted_only
            else candidates
        )
        if not eligible:
            statuses = sorted({str(item.manifest["status"]) for item in candidates})
            raise ManifestLookupError(
                "matching consensus manifest is not accepted "
                f"(status: {', '.join(statuses)})"
            )
        if len(eligible) != 1:
            matches = ", ".join(
                str(item.path.relative_to(self.root)) for item in eligible
            )
            raise ManifestLookupError(f"consensus manifest lookup is ambiguous: {matches}")
        return eligible[0]

    load = lookup


def load_validated_bars(
    root: str | Path,
    symbol: str,
    *,
    market: str | None = None,
    interval: str | None = None,
    accepted_only: bool = True,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_bars: int = DEFAULT_MAX_BARS,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> ValidatedBars:
    """Load one verified identity from an offline manifest directory."""

    store = ValidatedBarsStore(
        root,
        max_files=max_files,
        max_file_bytes=max_file_bytes,
        max_bars=max_bars,
        max_entries=max_entries,
    )
    return store.lookup(
        symbol,
        market=market,
        interval=interval,
        accepted_only=accepted_only,
    )


__all__ = [
    "MANIFEST_SCHEMA",
    "ManifestLookupError",
    "ManifestValidationError",
    "StoreConfigurationError",
    "ValidatedBars",
    "ValidatedBarsError",
    "ValidatedBarsStore",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "load_validated_bars",
]
