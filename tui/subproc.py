"""Bounded subprocess runner shared by operator-configured domain commands.

One discipline for every command the TUI spawns directly: a fresh process
group per child (so a timeout kills the whole tree, not just the direct
child), explicit UTF-8 decoding independent of the ambient locale, and no
shell.
"""

from __future__ import annotations

import os
import signal
import subprocess
from typing import Any


def run_bounded(
    argv: list[str], timeout: float, label: str = "command", *, text: bool = True
) -> dict[str, Any]:
    """Run argv without a shell; {"ok": bool, "error": str, "stdout": str|bytes}.

    Errors are prefixed with ``label`` so each caller keeps its own wording.
    With ``text=True`` (default) output is decoded as UTF-8 with replacement
    characters so a non-UTF-8 locale cannot turn valid JSON into a decode
    failure; ``text=False`` keeps raw bytes for callers that parse strict
    JSON themselves.
    """
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
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        _stdout, _stderr = proc.communicate()
        return {"ok": False, "error": f"{label} timed out after {timeout}s", "stdout": b""}
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
