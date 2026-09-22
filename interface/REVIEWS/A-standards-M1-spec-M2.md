# Review A — Standards (M1) / Spec (M2)

Adversarial two-axis review of the `interface/` package (whole tree is the diff).
All claims verified by reading code, running ruff, running pytest, and probing
unpinned semantics with live snippets. Axes are reported separately, not merged.

## Standards (M1 files)

### (a) Documented-standard violations

1. **Silent fallback to fake data from the CLI (worst).** AGENTS.md:56-58 —
   "Fail closed... no silent fallback to fake adapters"; panels.py:6 —
   "Never-fake-data". `app/shell.py:183-186` and `watch.py:163-166` define
   `--demo` as `action="store_true", default=True` with no `--no-demo`, so
   `args.demo` is always True, `build_provider(root, demo=True)` ignores
   `--root`, and `python -m interface.app.shell --root PATH` silently renders
   synthetic data. The help text still says "(demo data by default until the
   M2 collectors land)" — M2 has landed; the real path is unreachable from
   both shipped CLIs (programmatic/tests only).
2. **Boundary rule broken at the composition roots.** interface/README.md:14-16
   — "UI modules import only `snapshot`, the `SnapshotProvider` protocol, and
   `render`. Everything else is a collector or a service." Yet
   `app/shell.py:27` and `watch.py:31` do `from interface.collectors import
   build_provider`, and `watch.py:188` imports `interface.status`. Wiring a
   provider somewhere is necessary, but the rule as written admits no
   exception and none was documented.
3. **README not maintained through M1.** The path table (interface/README.md
   :6-12) has no rows for `app/`, `screens/`, `watch.py`. The documented test
   command (README:24-26, `uv run --no-project --with pytest --python 3.11 --
   python -m pytest interface/tests/ -q`) now fails collection on the M1 tests
   (`ModuleNotFoundError: No module named 'textual'`); it needs `--with
   textual --with rich` (with them: 16 passed).
4. **Hint convention drops on overrides.** `overview.py:80,91,103`
   (`compose`/`on_mount`/`on_unmount`) are unhinted/undocstringed while the
   rest of the package (snapshot.py, spine.py, watch.py) hints everything.

Verified clean: ruff 0.16.8 (default rules + explicit E501) at line-length 100
passes on all M1 files; no hex literal outside `render/tokens.py`; no `tui/`
imports; English throughout; M1 behavior ships with tests (spine/pilot/count).

### (b) Baseline smells (judgement calls)

- **Duplicated Code** — `_parse_args` in `shell.py:177-189` and
  `watch.py:157-172` repeat the same `--demo/--root/--interval` block and the
  same stale copy; extract a shared fragment.
- **Duplicated Code** — bare-table incantation `Table(box=None, pad_edge=False,
  show_edge=False)` at `panels.py:121,206,222` plus the em-dash empty-row
  pattern at `panels.py:210,226`; extract `_bare_table(headers)`.
- **Middle Man** — `spine.py:288-289` `_panel_multiplier` is a one-line
  delegation to `self._cadence.multiplier` with a single caller.
- **Duplicated bookkeeping** — `update_counts` incremented in both
  `spine.py:317-319` and `overview.py:138-140`; the pilot cross-check
  (test_cadence_counts.py:72-76) makes it deliberate, but the screen could
  read the spine's counts instead of re-counting.
- **Primitive Obsession** (weak; grep-friendly is documented intent) — panel
  ids and staleness levels travel as bare strings across spine/overview/watch.

**Summary: 4 documented violations + 5 baseline smells; worst: both shipped
CLIs accept `--root` but cannot stop showing demo data — a silent fallback to
fake data that AGENTS.md rule 2 forbids.**

## Spec (M2 work)

Audit against the given M2 brief; the commanded pytest run passes:
**51 passed in 0.32s**.

1. **events.py — verified, one partial.** Discovery `<root>/runs/*/`
   (events.py:230-232); byte-offset cursor; complete lines only; the
   "advance ONLY after the line parsed and the frame built" rule holds beyond
   the tests: a mid-frame parse failure leaves the cursor exactly at the
   corrupt line's start (probe: cursor 308 == 308), torn lines re-read,
   blank lines tolerated-and-consumed; all 16 types match
   `schemas/run-event.schema.json`; tail `deque(maxlen=200)` ascending;
   corrupt/missing/empty/shrunk → `state="corrupt"`, never raises;
   cost → CostSnapshot. **Partial:** `run.cancelled` leaves a started stage
   `"running"` forever (events.py:66-67 `_STAGE_STATE_ON_RUN_TERMINAL` omits
   `"cancelled"`; applied at 361-367) while blocked/halted/failed mark their
   running stages — the wall shows a live stage under a cancelled run. Spec:
   "fold all 16 run-event types into RunSnapshot.state (created→staged→
   running→gating→completed/blocked/failed/halted; cancelled/reconciled/
   resumed)" — stage projection of cancelled is unspecified; flagged.
2. **files.py — verified.** capturing→interrupted (files.py:102-107); settable
   hook, default none, raising degrades (files.py:121); quantkit find_spec
   (files.py:45); ai "env-variable-NAME presence ONLY" — membership test,
   values never read (files.py:57). Fidelity note: `_AI_ENV_KEYS`
   (files.py:35-39) drops `ANTHROPIC_API_KEY` vs the old plane
   (tui/config.py:53-62); the spec doesn't enumerate names.
3. **core_cli.py — verified, one strictness gap.** Spec: "strict
   {ok,command,data,error} envelope" — but a payload with no `command` field
   parses `ok=True` (core_cli.py:121-124 falls back to `op`); probe:
   `{"ok": true, "data": {}}` → ok=True. Lookup order, STAMMTISCH_HOME,
   whitelist raising in `_execute` (core_cli.py:205-209) plus init/run/delete
   (171-180), 15s TTL single-spawn, budget accounting, missing-binary
   `ServiceStatus(core, False)` all as specified and pinned. Note: the
   status()/TTL lane has no shipped caller (session.py:59 uses only
   `service_status()`) — implemented per spec, exercised only by tests.
4. **session/__init__ — verified.** Registry keyed (resolved root, use_core)
   (session.py:71-85); cursors survive; live-first-then-newest; `quotes=()`;
   services core/quantkit/ai; error join; demo path untouched; the root=None
   frame keeps "M2" and is locked — by frozen `test_status.py:52-54`.
   Judgement note: corrupt runs sort as live (session.py:40 — `corrupt` not
   in TERMINAL_RUN_STATES) and pin to the top of the wall forever.
5. **Tests — verified.** Every enumerated behavior present, including LIVE
   append-between-frames with `bytes_read` delta == appended bytes; fixtures
   synthetic (tmp_path only); no real-root writes; no key values; shipped
   code imports nothing from `tui/` (grep empty).
6. **Accepted deviations** (currency "tokens"; record digest in
   `artifact_sha256`; reconciled/resumed named states; `intake-sessions/`)
   implemented exactly as accepted — not counted.

**Summary: 2 findings (cancelled leaves stage "running"; envelope `command`
not strictly required) + 3 notes; worst: `run.cancelled` never retires its
running stage, so the wall shows a live stage under a cancelled run forever.**
