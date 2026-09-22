#!/usr/bin/env python3
"""Fake stammtisch-core for tests: speaks the {ok, command, data, error} envelope.

Behavior is selected with FAKE_CORE_MODE:

  ok      -> ok envelope; data.runs from FAKE_CORE_RUNS (JSON list),
             data.state_root echoes the received STAMMTISCH_HOME;
             op "delete" answers ok with data.removed true
  error   -> ok:false envelope with an error object, exit 1 (any op)
  garbage -> non-JSON stdout, exit 0
  exit    -> human stderr only, exit 3 (no envelope)

Supported ops: ``status`` (read lane) and ``delete <run-id>`` (the M5
write lane — set FAKE_CORE_DELETE_FAIL to a run id that must fail with
ok:false "run unknown"). Everything else exits 3 with human stderr, the
way the real core refuses unknown ops. When FAKE_CORE_LOG is set, every
invocation appends one line to that file so tests can count spawns and
verify the exact argv of a delete independently of the client's own
accounting.
"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    argv = sys.argv[1:]
    mode = os.environ.get("FAKE_CORE_MODE", "ok")
    log = os.environ.get("FAKE_CORE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(f"{time.time():.3f} {' '.join(argv)}\n")
    op = argv[0] if argv else ""
    if op not in ("status", "delete"):
        sys.stderr.write(f"fake core: unsupported op {argv!r}\n")
        return 3
    if mode == "garbage":
        sys.stdout.write("this is not json\n")
        return 0
    if mode == "exit":
        sys.stderr.write("fake core: human-readable failure\n")
        return 3
    if mode == "error":
        sys.stdout.write(json.dumps({
            "ok": False,
            "command": op,
            "data": {},
            "error": {"code": "run_unknown",
                      "message": "no run with id 'example-run'"},
        }) + "\n")
        return 1
    if op == "delete":
        run_id = argv[1] if len(argv) > 1 else ""
        fail_id = os.environ.get("FAKE_CORE_DELETE_FAIL")
        if not run_id or (fail_id and run_id == fail_id):
            sys.stdout.write(json.dumps({
                "ok": False,
                "command": "delete",
                "data": {},
                "error": {"code": "run_unknown",
                          "message": f"no run with id '{run_id}'"},
            }) + "\n")
            return 1
        sys.stdout.write(json.dumps({
            "ok": True,
            "command": "delete",
            "data": {"removed": True, "run_id": run_id},
        }) + "\n")
        return 0
    runs = json.loads(os.environ.get("FAKE_CORE_RUNS", "[]"))
    sys.stdout.write(json.dumps({
        "ok": True,
        "command": "status",
        "data": {"state_root": os.environ.get("STAMMTISCH_HOME", ""),
                 "runs": runs},
    }) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
