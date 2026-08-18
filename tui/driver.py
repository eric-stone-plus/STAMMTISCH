"""Driver — wraps the `stammtisch` CLI binary, parses JSON envelopes."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_STAGE_TIMEOUT_SECONDS = 3600
# Absolute ceiling for the auto-derived run budget: a pipeline declaring
# hours of stage timeouts must not pin a TUI run command for hours with no
# way to interrupt it. Operators can still pass an explicit timeout.
MAX_PIPELINE_RUN_TIMEOUT_SECONDS = 1800
DEFAULT_STAGE_POLL_SECONDS = 30
# Poll requests and sleeps are clamped to the core stage deadline. Preflight,
# invoke and the final collect remain outside that poll budget, each with the
# client's 120-second read cap; reserve three calls so the outer owner never
# kills a healthy local core while its remote task remains uncancelled.
A2A_HTTP_READ_TIMEOUT_SECONDS = 120
A2A_NON_POLL_CALLS_PER_STAGE = 3
RUN_BASE_OVERHEAD_SECONDS = 60
RUN_STAGE_OVERHEAD_SECONDS = 5


def _find_binary() -> str:
    """Locate the stammtisch binary."""
    # Check env first
    if env := os.environ.get("STAMMTISCH_BIN"):
        if Path(env).exists():
            return env
    # Check relative to this file's repo root
    repo = Path(__file__).resolve().parent.parent
    release_core = repo / "target" / "release" / "stammtisch-core"
    release = repo / "target" / "release" / "stammtisch"
    debug_core = repo / "target" / "debug" / "stammtisch-core"
    debug = repo / "target" / "debug" / "stammtisch"
    for candidate in [release_core, release, debug_core, debug]:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    # Fall back to PATH
    return "stammtisch"


def _find_state_root() -> str | None:
    """Resolve state root from env or default."""
    if home := os.environ.get("STAMMTISCH_HOME"):
        return home
    default = Path.home() / ".local" / "share" / "stammtisch"
    if default.is_dir():
        return str(default)
    return None


@dataclass
class CommandResult:
    """Parsed output from a stammtisch --json invocation."""
    ok: bool
    command: str
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    raw: str = ""
    returncode: int = 0

    @property
    def error_message(self) -> str | None:
        if self.error:
            return self.error.get("message", str(self.error))
        return None


class StammtischDriver:
    """Thin wrapper around the stammtisch CLI binary."""

    def __init__(
        self,
        binary: str | None = None,
        state_root: str | None = None,
        pipeline_dir: str | None = None,
    ):
        self.binary = binary or _find_binary()
        self.state_root = state_root or _find_state_root()
        self.pipeline_dir = pipeline_dir or str(
            Path(__file__).resolve().parent.parent / "pipelines" / "examples"
        )
        self._process_lock = threading.Lock()
        self._processes: set[subprocess.Popen[str]] = set()
        self._closed = False

    @staticmethod
    def _stop_process(proc: subprocess.Popen[str]) -> None:
        """Stop one local process group started by this driver."""
        if proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:  # pragma: no cover - non-POSIX only
                proc.terminate()
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=2)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover - non-POSIX only
                proc.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def close(self) -> None:
        """Stop only the local CLI processes owned by this driver.

        Wire-backed products may continue remotely; the driver never sends an
        A2A cancellation. Reconcile binds the durable local state afterward.
        """
        with self._process_lock:
            self._closed = True
            processes = tuple(self._processes)
        for proc in processes:
            self._stop_process(proc)

    def _run(
        self,
        args: list[str],
        timeout: float | None = 30,
    ) -> CommandResult:
        """Execute stammtisch with --json and parse the envelope."""
        cmd = [self.binary] + args + ["--json"]
        env = os.environ.copy()
        if self.state_root:
            env["STAMMTISCH_HOME"] = self.state_root

        try:
            with self._process_lock:
                if self._closed:
                    return CommandResult(
                        ok=False,
                        command=args[0] if args else "?",
                        error={"message": "driver is closed"},
                        returncode=125,
                    )
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    start_new_session=os.name == "posix",
                )
                self._processes.add(proc)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._stop_process(proc)
                try:
                    stdout, stderr = proc.communicate(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    stdout, stderr = "", ""
                label = "unbounded" if timeout is None else f"{timeout:g}s"
                return CommandResult(
                    ok=False,
                    command=args[0] if args else "?",
                    error={
                        "message": (
                            f"local core timeout after {label}; its process was "
                            "stopped, but any remote product was not cancelled; "
                            "run reconcile before launching again"
                        )
                    },
                    returncode=124,
                    raw=(stdout or "") + (stderr or ""),
                )
            finally:
                with self._process_lock:
                    self._processes.discard(proc)
        except FileNotFoundError:
            return CommandResult(
                ok=False, command=args[0] if args else "?",
                error={"message": f"binary not found: {self.binary}"},
                returncode=127,
            )
        except (OSError, UnicodeDecodeError) as e:
            return CommandResult(
                ok=False, command=args[0] if args else "?",
                error={"message": f"spawn/read failed: {e}"},
                returncode=126,
            )

        raw = stdout.strip()
        if not raw:
            return CommandResult(
                ok=False, command=args[0] if args else "?",
                error={"message": stderr.strip() or "no output"},
                returncode=proc.returncode,
                raw=stderr,
            )

        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            return CommandResult(
                ok=False, command=args[0] if args else "?",
                error={"message": f"unparseable JSON: {raw[:200]}"},
                returncode=proc.returncode,
                raw=raw,
            )
        if not isinstance(envelope, dict):
            return CommandResult(
                ok=False, command=args[0] if args else "?",
                error={"message": f"envelope is not an object: {raw[:200]}"},
                returncode=proc.returncode,
                raw=raw,
            )
        if not isinstance(envelope.get("ok"), bool):
            return CommandResult(
                ok=False, command=args[0] if args else "?",
                error={"message": "envelope has no boolean ok field"},
                returncode=proc.returncode,
                raw=raw,
            )
        data = envelope.get("data", {})
        error = envelope.get("error")
        if not isinstance(data, dict) or (error is not None and not isinstance(error, dict)):
            return CommandResult(
                ok=False, command=args[0] if args else "?",
                error={"message": "envelope data/error shape is invalid"},
                returncode=proc.returncode,
                raw=raw,
            )
        return CommandResult(
            ok=envelope["ok"],
            command=envelope.get("command", args[0] if args else "?"),
            data=data,
            error=error,
            raw=raw,
            returncode=proc.returncode,
        )

    @staticmethod
    def _pipeline_run_timeout(pipeline_path: str) -> float:
        """Derive a safe outer CLI budget from every declared stage budget."""
        stage_budgets: list[int] = []
        try:
            value = json.loads(Path(pipeline_path).read_text(encoding="utf-8"))
            stages = value.get("stages", []) if isinstance(value, dict) else []
            if isinstance(stages, list):
                for stage in stages:
                    declared = (
                        stage.get("timeout_seconds", DEFAULT_STAGE_TIMEOUT_SECONDS)
                        if isinstance(stage, dict)
                        else DEFAULT_STAGE_TIMEOUT_SECONDS
                    )
                    if (
                        isinstance(declared, bool)
                        or not isinstance(declared, int)
                        or declared < 1
                    ):
                        declared = DEFAULT_STAGE_TIMEOUT_SECONDS
                    poll_seconds = (
                        stage.get("poll_seconds", DEFAULT_STAGE_POLL_SECONDS)
                        if isinstance(stage, dict)
                        else DEFAULT_STAGE_POLL_SECONDS
                    )
                    if (
                        isinstance(poll_seconds, bool)
                        or not isinstance(poll_seconds, int)
                        or poll_seconds < 0
                    ):
                        poll_seconds = DEFAULT_STAGE_POLL_SECONDS
                    runtime = stage.get("runtime") if isinstance(stage, dict) else None
                    transport_allowance = (
                        A2A_HTTP_READ_TIMEOUT_SECONDS * A2A_NON_POLL_CALLS_PER_STAGE
                        if isinstance(runtime, dict) and runtime.get("protocol") == "a2a"
                        else 0
                    )
                    stage_budgets.append(declared + transport_allowance)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        if not stage_budgets:
            stage_budgets = [DEFAULT_STAGE_TIMEOUT_SECONDS]
        return min(
            MAX_PIPELINE_RUN_TIMEOUT_SECONDS,
            float(
                sum(stage_budgets)
                + RUN_BASE_OVERHEAD_SECONDS
                + RUN_STAGE_OVERHEAD_SECONDS * len(stage_budgets)
            ),
        )

    # ── Commands ────────────────────────────────────────────────

    def init(self) -> CommandResult:
        return self._run(["init"])

    def validate(self, pipeline_path: str) -> CommandResult:
        return self._run(["validate", "--pipeline", pipeline_path])

    def run(self, pipeline_path: str, timeout: float | None = None) -> CommandResult:
        budget = self._pipeline_run_timeout(pipeline_path) if timeout is None else timeout
        return self._run(["run", "--pipeline", pipeline_path], timeout=budget)

    def status(self, run_id: str | None = None) -> CommandResult:
        args = ["status"]
        if run_id:
            args.append(run_id)
        return self._run(args)

    def inspect(self, run_id: str) -> CommandResult:
        return self._run(["inspect", run_id])

    def reconcile(self) -> CommandResult:
        return self._run(["reconcile"])

    def export(self, run_id: str, out_dir: str) -> CommandResult:
        return self._run(["export", run_id, "--out", out_dir])

    def verify(self, bundle_dir: str) -> CommandResult:
        return self._run(["verify", "--bundle", bundle_dir])

    def delete(self, run_id: str, force: bool = False) -> CommandResult:
        args = ["delete", run_id]
        if force:
            args.append("--force")
        return self._run(args)

    # ── Convenience ─────────────────────────────────────────────

    def list_pipelines(self) -> list[Path]:
        """List available pipeline spec files."""
        d = Path(self.pipeline_dir)
        if not d.is_dir():
            return []
        return sorted(d.glob("*.json"))

    def list_runs(self) -> list[dict[str, Any]]:
        """Get all runs via status command."""
        result = self.status()
        if result.ok:
            return result.data.get("runs", [])
        return []

    def get_state_root(self) -> str | None:
        return self.state_root

    def is_initialized(self) -> bool:
        if not self.state_root:
            return False
        p = Path(self.state_root)
        return (p / "runs").is_dir() and (p / "host").is_dir()
