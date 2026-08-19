"""Bounded subprocess runner shared by operator-configured domain commands.

One discipline for every command the TUI spawns directly: a fresh process
group per child (so a timeout kills the whole tree, not just the direct
child), explicit UTF-8 decoding independent of the ambient locale, and no
shell. Children spawned with ``parent_death=True`` (or reaped by
``stop_owned``) also carry the no-residue shutdown contract: when the TUI
dies — gracefully or to a kill -9 — the child dies with it instead of
continuing detached.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any


_PARENT_POLL_SECONDS = 2.0


def run_bounded(
    argv: list[str],
    timeout: float,
    label: str = "command",
    *,
    text: bool = True,
    parent_death: bool = False,
) -> dict[str, Any]:
    """Run argv without a shell; {"ok": bool, "error": str, "stdout": str|bytes}.

    Errors are prefixed with ``label`` so each caller keeps its own wording.
    With ``text=True`` (default) output is decoded as UTF-8 with replacement
    characters so a non-UTF-8 locale cannot turn valid JSON into a decode
    failure; ``text=False`` keeps raw bytes for callers that parse strict
    JSON themselves.

    ``parent_death=True`` reruns the command under the module's parent-death
    shim: a wrapper process that polls the TUI's pid and kills the command's
    whole group the moment the TUI is gone (same contract the core CLI gets
    via ``STAMMTISCH_PARENT_PID`` and the chart server via ``--parent-pid``).
    """
    if parent_death and hasattr(os, "getppid"):
        argv = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--parent-pid",
            str(os.getpid()),
            "--",
            *argv,
        ]
    try:
        proc = subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        return {"ok": False, "error": f"{label} not found: {argv[0]}", "stdout": b""}
    except OSError as e:
        return {"ok": False, "error": f"{label} spawn failed: {e}", "stdout": b""}
    with _registry_lock:
        _owned_processes.add(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        _stdout, _stderr = proc.communicate()
        return {"ok": False, "error": f"{label} timed out after {timeout}s", "stdout": b""}
    finally:
        with _registry_lock:
            _owned_processes.discard(proc)
    if text:
        stdout = stdout.decode("utf-8", errors="replace")
        stderr = stderr.decode("utf-8", errors="replace")
    return {
        "ok": True,
        "error": "",
        "stdout": stdout,
        "stderr": stderr,
        "returncode": proc.returncode,
    }


# Children still in flight, so a TUI shutdown can reap them instead of
# leaving them detached (mirrors the driver's and chart server's
# ownership registries).
_owned_processes: set[subprocess.Popen[bytes]] = set()
_registry_lock = threading.Lock()


def stop_owned() -> None:
    """Terminate every command this process still has in flight.

    Called from the TUI's unmount hook: graceful shutdown leaves no child
    behind. Abrupt deaths (kill -9, lost terminal) are covered by the
    parent-death shim instead.
    """
    with _registry_lock:
        procs = list(_owned_processes)
        _owned_processes.clear()
    for proc in procs:
        _kill_tree(proc)


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Terminate the child's whole process group, escalating to SIGKILL.

    The child is a session leader (``start_new_session=True``), so its pid
    names the process group; grandchildren spawned by the command die with
    it instead of leaking after the TUI moves on.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()


def _kill_own_group() -> None:
    """SIGKILL this process's group (the shim's parent-loss escape)."""
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _watched_exec(parent_pid: int, argv: list[str]) -> int:
    """Shim main: run argv in this process's group; die with the parent.

    The command inherits the shim's process group (the shim itself is the
    session leader started by ``run_bounded``), so killing the group takes
    the command and any of its grandchildren down together — and a
    ``run_bounded`` timeout kill reaches them through the same group.
    """
    if os.getppid() != parent_pid:
        # The parent died between spawn and this point; do not start.
        return 125
    try:
        proc = subprocess.Popen(argv)
    except FileNotFoundError:
        print(f"subproc: command not found: {argv[0]}", file=sys.stderr)
        return 127
    except OSError as exc:
        print(f"subproc: spawn failed: {exc}", file=sys.stderr)
        return 126
    stop = threading.Event()

    def _watch() -> None:
        while not stop.wait(_PARENT_POLL_SECONDS):
            if os.getppid() != parent_pid:
                # The TUI is gone and nobody will verify this command's
                # output; take the whole group down with us.
                _kill_own_group()

    threading.Thread(target=_watch, name="subproc-parent-watch", daemon=True).start()
    try:
        return proc.wait()
    finally:
        stop.set()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4 or args[0] != "--parent-pid" or args[2] != "--":
        print(
            "usage: subproc.py --parent-pid PID -- COMMAND [ARGS...]",
            file=sys.stderr,
        )
        return 2
    try:
        parent_pid = int(args[1])
    except ValueError:
        print("subproc: --parent-pid must be an integer", file=sys.stderr)
        return 2
    return _watched_exec(parent_pid, args[3:])


if __name__ == "__main__":
    raise SystemExit(main())
