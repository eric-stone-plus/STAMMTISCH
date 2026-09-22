"""CoreCliClient — the budgeted, fail-closed adapter to the core binary.

The rebuilt interface keeps the old discipline: the ONLY core contact is
a ``stammtisch-core`` subprocess speaking the ``{ok, command, data,
error}`` envelope, strictly validated client-side (non-object, missing
boolean ``ok``, or non-dict ``data``/``error`` shapes all fail closed).
What changed at M2 is the cadence: this client is the ×15 lane, so reads
carry a TTL cache (one spawn per 15s window) and spawn accounting, and
the whole snapshot plane never blocks on a spawn.

Whitelist is client-side and STRICT: ``status`` is the read op, and
since M5 ``delete`` is the sole WRITE op — reachable only through the
audited ``:delete`` confirm path (ConfirmDialog -> off-thread worker ->
activity-feed audit lines). ``delete`` never touches the TTL cache (a
confirmed write must always reach the core) and is not gated on the
read lane's spawn budget (its spawns still count in ``spawn_count``).
Every other write path (init/run/...) still raises ``NotImplementedError``
instead of spawning.

A missing binary is a service fact, not an exception: availability
degrades to ``ServiceStatus(core, False, ...)`` and never raises into
the snapshot.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from interface.snapshot import ServiceStatus

SERVICE_NAME = "core"

#: One spawn per status window — the ×15 lane contract (counting-test locked).
STATUS_TTL_S = 15.0

#: Strict client-side op whitelist. ``status`` is the READ op (TTL-cached);
#: ``delete`` is the sole WRITE op (M5, audited confirm path only).
READ_OPS: frozenset[str] = frozenset({"status"})

#: The one sanctioned write. Everything outside ``READ_OPS | WRITE_OPS``
#: raises ``NotImplementedError`` before any spawn.
WRITE_OPS: frozenset[str] = frozenset({"delete"})

#: Hard wall-clock budget for one read spawn (fail closed, never pin a frame).
SPAWN_TIMEOUT_S = 10.0

_ERR_BINARY_MISSING = "core binary not found"


@dataclass(frozen=True)
class CliEnvelope:
    """One parsed {ok, command, data, error} frame from the core binary."""

    ok: bool
    command: str
    data: dict[str, Any] = field(default_factory=dict)
    error_message: str = ""
    returncode: int = 0


def find_core_binary(explicit: str | None = None) -> str | None:
    """Locate the core binary (the old driver's lookup order, copied).

    ``STAMMTISCH_BIN`` env first, then the repo's release/debug build
    dirs (core name preferred), then PATH. Returns None when nothing
    resolves — availability is a fact, not an assumption.
    """
    if explicit:
        return explicit
    env = os.environ.get("STAMMTISCH_BIN")
    if env and Path(env).exists():
        return env
    repo = Path(__file__).resolve().parents[2]
    for candidate in (
        repo / "target" / "release" / "stammtisch-core",
        repo / "target" / "release" / "stammtisch",
        repo / "target" / "debug" / "stammtisch-core",
        repo / "target" / "debug" / "stammtisch",
    ):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("stammtisch-core") or shutil.which("stammtisch")


def default_state_root() -> Path | None:
    """Resolve the state root from STAMMTISCH_HOME or the shared default."""
    if home := os.environ.get("STAMMTISCH_HOME"):
        return Path(home)
    shared = Path.home() / ".local" / "share" / "stammtisch"
    return shared if shared.is_dir() else None


def _parse_envelope(op: str, stdout: str, stderr: str,
                    returncode: int) -> CliEnvelope:
    """Strict envelope validation; any shape violation fails closed."""
    raw = stdout.strip()
    if not raw:
        message = stderr.strip() or "no output"
        return CliEnvelope(False, op, error_message=message,
                           returncode=returncode)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return CliEnvelope(False, op,
                           error_message=f"unparseable JSON: {raw[:200]}",
                           returncode=returncode)
    if not isinstance(payload, dict):
        return CliEnvelope(False, op,
                           error_message=f"envelope is not an object: {raw[:200]}",
                           returncode=returncode)
    if not isinstance(payload.get("ok"), bool):
        return CliEnvelope(False, op,
                           error_message="envelope has no boolean ok field",
                           returncode=returncode)
    data = payload.get("data", {})
    error = payload.get("error")
    if not isinstance(data, dict) or (error is not None
                                      and not isinstance(error, dict)):
        return CliEnvelope(False, op,
                           error_message="envelope data/error shape is invalid",
                           returncode=returncode)
    message = str(error.get("message") or error) if error is not None else ""
    command = payload.get("command")
    if payload["ok"] and (not isinstance(command, str) or not command):
        # Adversarial review A: a missing command field on an ok frame used
        # to pass with the op name substituted — strict means strict.
        return CliEnvelope(False, op,
                           error_message="envelope has no command field",
                           returncode=returncode)
    return CliEnvelope(
        payload["ok"],
        command if isinstance(command, str) and command else op,
        data=data,
        error_message=message,
        returncode=returncode,
    )


class CoreCliClient:
    """Core-CLI adapter: TTL-cached reads plus ONE audited write (delete)."""

    def __init__(self, root: Path | None = None, binary: str | None = None,
                 ttl_s: float = STATUS_TTL_S,
                 spawn_budget: int | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.root = root if root is not None else default_state_root()
        self.binary = binary if binary is not None else find_core_binary()
        self.ttl_s = ttl_s
        self.spawn_budget = spawn_budget
        self._clock = clock
        self._cached: tuple[float, CliEnvelope] | None = None
        #: Spawn accounting: every subprocess this client started, lifetime.
        self.spawn_count = 0

    # ── reads (whitelisted) ────────────────────────────────────────

    def status(self, run_id: str | None = None,
               *, now: float | None = None) -> CliEnvelope:
        """Envelope for ``status``; one spawn per TTL window (cached between).

        ``now`` overrides the clock for tests; production callers take the
        monotonic default.
        """
        current = self._clock() if now is None else now
        if self._cached is not None:
            stamp, cached = self._cached
            if current - stamp < self.ttl_s:
                return cached
        if self.spawn_budget is not None and self.spawn_count >= self.spawn_budget:
            return CliEnvelope(False, "status",
                               error_message="spawn budget exhausted",
                               returncode=125)
        result = self._execute("status", [run_id] if run_id else [])
        self._cached = (current, result)
        return result

    # ── writes (sole op: delete; audited confirm path only) ────────

    def delete(self, run_id: str) -> CliEnvelope:
        """Envelope for ``delete <run-id>`` — the ONE WRITE op (M5).

        Never cached and never budget-gated: a confirmed delete must
        always reach the core (its spawn still counts in ``spawn_count``).
        Success is decided by the core's ``{ok}`` envelope alone; a truthy
        ``data.removed`` echoes into the caller's audit line. An empty id
        fails closed without spawning.
        """
        if not run_id:
            return CliEnvelope(False, "delete",
                               error_message="no run id given",
                               returncode=2)
        return self._execute("delete", [run_id])

    def init(self) -> CliEnvelope:
        raise NotImplementedError("core init is a write op; not exposed")

    def run(self, pipeline_path: str) -> CliEnvelope:
        del pipeline_path
        raise NotImplementedError("core run is a write op; not exposed")

    # ── services ───────────────────────────────────────────────────

    def service_status(self) -> ServiceStatus:
        """Fail-closed availability row; never raises into the snapshot.

        Availability means resolvable-and-executable, verified without a
        spawn: a path-like binary must exist and carry the exec bit, a
        bare name must resolve on PATH.
        """
        if not self.binary:
            return ServiceStatus(SERVICE_NAME, False, _ERR_BINARY_MISSING)
        if os.sep in self.binary or Path(self.binary).is_absolute():
            if Path(self.binary).exists() and os.access(self.binary, os.X_OK):
                return ServiceStatus(SERVICE_NAME, True, self.binary)
            return ServiceStatus(SERVICE_NAME, False,
                                 f"binary not executable: {self.binary}")
        if shutil.which(self.binary):
            return ServiceStatus(SERVICE_NAME, True, self.binary)
        return ServiceStatus(SERVICE_NAME, False,
                             f"binary not on PATH: {self.binary}")

    # ── spawning ───────────────────────────────────────────────────

    def _execute(self, op: str, args: list[str]) -> CliEnvelope:
        allowed = READ_OPS | WRITE_OPS
        if op not in allowed:
            raise NotImplementedError(
                f"core op '{op}' is outside the client whitelist "
                f"{sorted(allowed)}; only the audited delete write exists")
        if not self.binary:
            return CliEnvelope(False, op, error_message=_ERR_BINARY_MISSING,
                               returncode=127)
        command = [self.binary, op, *args, "--json"]
        env = os.environ.copy()
        if self.root is not None:
            env["STAMMTISCH_HOME"] = str(self.root)
        self.spawn_count += 1
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=env,
                timeout=SPAWN_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError:
            return CliEnvelope(
                False, op, error_message=f"binary not found: {self.binary}",
                returncode=127)
        except subprocess.TimeoutExpired:
            return CliEnvelope(False, op,
                               error_message=f"core {op} timed out",
                               returncode=124)
        return _parse_envelope(op, completed.stdout or "",
                               completed.stderr or "", completed.returncode)
