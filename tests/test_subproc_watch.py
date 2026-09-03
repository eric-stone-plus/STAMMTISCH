"""No-residue shutdown and locked-intake-delete regression tests.

Covers the intake product's parent-death contract (the subproc shim and
the ownership registry) and the IntakeSupervisor's locked delete paths:
a delete must never clobber a capture that is live or started later.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tui.intake_job import IntakeSupervisor, empty_session, save_session, session_path
from tui.subproc import main, run_bounded, stop_owned

ROOT = Path(__file__).resolve().parents[1]


def _proc_running(pid: int) -> bool:
    """True while pid exists and is not a zombie (empty cmdline)."""
    try:
        return bool(Path(f"/proc/{pid}/cmdline").read_bytes())
    except OSError:
        return False


def _wait_until(predicate, budget: float = 10.0, step: float = 0.1) -> bool:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class SubprocShimTest(unittest.TestCase):
    def test_usage_errors_fail_fast(self) -> None:
        self.assertEqual(main([]), 2)
        self.assertEqual(main(["--parent-pid", "1", "x", "--", "true"]), 2)
        self.assertEqual(main(["--parent-pid", "not-a-pid", "--", "true"]), 2)

    def test_shim_never_starts_the_command_when_parent_is_already_gone(self) -> None:
        # A pid that cannot be this process's parent: the shim must
        # refuse to launch the command at all.
        stranger = os.getppid() + 1
        started = time.monotonic()
        code = main([
            "--parent-pid",
            str(stranger),
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(10)",
        ])
        self.assertEqual(code, 125)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_parent_death_kills_the_command_group(self) -> None:
        """A launcher process dies mid-command; the shim must take the
        command down with it instead of leaving it detached to write the
        workspace alone."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child.pid"
            launcher = Path(tmp) / "launcher.py"
            child_code = (
                "import os, time; "
                f"open({str(marker)!r}, 'w').write(str(os.getpid())); "
                "time.sleep(120)"
            )
            launcher.write_text(
                "\n".join([
                    "import sys, threading, time",
                    f"sys.path.insert(0, {str(ROOT)!r})",
                    "from tui.subproc import run_bounded",
                    f"child = {[sys.executable, '-c', child_code]!r}",
                    "threading.Thread(",
                    "    target=lambda: run_bounded("
                    "        child, 120, 'watched', parent_death=True"
                    "    ),",
                    "    daemon=True,",
                    ").start()",
                    "deadline = time.monotonic() + 5.0",
                    "while time.monotonic() < deadline:",
                    "    time.sleep(0.05)",
                    # The launcher (the shim's TUI stand-in) dies here
                    # without reaping anything.
                ]),
                encoding="utf-8",
            )
            finished = subprocess.run(
                [sys.executable, str(launcher)],
                timeout=30,
                check=False,
            )
            self.assertEqual(finished.returncode, 0)
            self.assertTrue(marker.exists(), "command never started")
            pid = int(marker.read_text(encoding="utf-8").strip())
            self.assertTrue(
                _wait_until(lambda: not _proc_running(pid), budget=12.0),
                "orphaned command survived the launcher's death",
            )

    def test_stop_owned_reaps_in_flight_commands(self) -> None:
        """Graceful shutdown (the unmount hook) must not leave a child
        command running detached either."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child.pid"
            code = (
                "import os, time; "
                f"open({str(marker)!r}, 'w').write(str(os.getpid())); "
                "time.sleep(120)"
            )
            def _work() -> None:
                run_bounded([sys.executable, "-c", code], 120, "owned")

            worker = threading.Thread(target=_work, daemon=True)
            worker.start()
            try:
                self.assertTrue(
                    _wait_until(marker.exists, budget=15.0),
                    "command never started",
                )
                pid = int(marker.read_text(encoding="utf-8").strip())
                self.assertTrue(_proc_running(pid))
                stop_owned()
                worker.join(timeout=15.0)
                self.assertFalse(worker.is_alive())
                self.assertTrue(
                    _wait_until(lambda: not _proc_running(pid), budget=12.0),
                    "stop_owned left the command running",
                )
            finally:
                stop_owned()  # no-op if already reaped


class IntakeSupervisorLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _install(self, job: IntakeSupervisor, session: dict) -> None:
        with job._lock:
            job.active = session

    def test_forget_never_clobbers_a_live_capture(self) -> None:
        job = IntakeSupervisor()
        session = empty_session(self.root, "20260819")
        self._install(job, session)
        self.assertTrue(job.is_capturing_session(session["id"]))
        self.assertFalse(job.forget(session["id"]), "capturing session forgotten")
        self.assertIsNotNone(job.active)
        # Terminal sessions are forgettable through the lock.
        session["state"] = "rejected"
        self.assertTrue(job.forget(session["id"]))
        self.assertIsNone(job.active)
        self.assertIsNone(job.result)

    def test_forget_of_a_finished_session_spares_a_newer_capture(self) -> None:
        job = IntakeSupervisor()
        stale = empty_session(self.root, "20260818")
        stale["state"] = "accepted"
        self._install(job, stale)
        # A new capture starts (same app) after the delete decision was
        # made against the stale snapshot.
        newer = empty_session(self.root, "20260819")
        self._install(job, newer)
        self.assertFalse(job.forget(stale["id"]))
        self.assertEqual(job.active.get("id"), newer["id"])
        self.assertEqual(job.active.get("state"), "capturing")
        self.assertTrue(job.is_capturing_session(newer["id"]))

    def test_is_capturing_session_checks_under_the_lock(self) -> None:
        job = IntakeSupervisor()
        session = empty_session(self.root, "20260819")
        self._install(job, session)
        self.assertFalse(job.is_capturing_session("some-other-id"))
        self.assertFalse(job.is_capturing_session(str(session["id"]) + "x"))
        # A state change is observed immediately (no stale snapshot).
        session["state"] = "accepted"
        self.assertFalse(job.is_capturing_session(session["id"]))

    def test_finished_session_delete_leaves_no_resurrectable_file(self) -> None:
        from tui.intake_job import delete_session

        job = IntakeSupervisor()
        session = empty_session(self.root, "20260819")
        session["state"] = "accepted"
        self._install(job, session)
        save_session(session)
        self.assertTrue(session_path(self.root, session["id"]).exists())
        self.assertTrue(delete_session(self.root, str(session["id"])))
        self.assertTrue(job.forget(str(session["id"])))
        self.assertFalse(session_path(self.root, session["id"]).exists())
        self.assertIsNone(job.active)


if __name__ == "__main__":
    unittest.main()
