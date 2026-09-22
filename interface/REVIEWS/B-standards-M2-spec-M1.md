# Review B — Standards on M2 / Spec on M1

Method: read every listed file; ran `uvx ruff@0.16.8 check` (default rules and
`--select E501 --line-length 100`) on the M2 files; ran
`uv run --no-project --with pytest --with textual==8.2.8 --with rich==15.0.0 --python 3.11 -- python -m pytest interface/tests/ -q`
→ **86 passed**. Three adversarial probes were executed (single-flight slot
leak, forced-every-frame mutation of the cadence test, Textual-free spine
import); noted inline.

## Standards (M2 files)

### (a) Documented-standard violations

1. **Dead error channel in FilesCollector — silent-empty degradation.**
   AGENTS.md:55–58, rule 2: "*Fail closed. Corrupt state … No best-effort
   parsing*" and the package's own contract, collectors/__init__.py:9–10:
   "*Every collector degrades: a missing state root yields empty facts with a
   `collector_error`*". files.py:74 returns a hardcoded `None` error, and a
   missing/unreadable `intake-sessions/` dir yields `()` with no error
   (files.py:86–90) — indistinguishable from "no sessions ever". The events
   collector treats the identical case as a collect-level error
   (`cannot scan`, events.py:233–234). The two collectors disagree on the
   documented contract. Low severity; row-level malformed-file leniency is
   documented and pinned by test (test_files_collector.py:92–102), so only the
   plane-level silence is flagged.

Verified clean (earned): boundary rule (interface/README.md:14–16) — import
scan shows collectors import only `interface.snapshot` and their own package;
secrecy — files.py:57 checks env-var *names* only, pinned by
test_files_collector.py:150–155 ("*The value NEVER leaves the env*"); ruff
clean (default rules and E501@100); English/no host paths (tmp_path fixtures
throughout); tests shipped with every behavior ("Tests are part of the
deliverable", AGENTS.md:64–65); "Events are the authority" — events.py folds
events.jsonl, no manifest read anywhere in collectors/. Nit: two test helpers
lack annotations (test_live_jsonl.py:48 `_live_run(frame)`;
test_files_collector.py:29 `_collect` missing return type).

### (b) Baseline smells (judgement calls)

1. **Duplicated Code** — test_events_collector.py:22–49 and
   test_live_jsonl.py:22–45 carry near-identical `_line`/`_created`/`_append`
   plus `SCHEMA`/`DIGEST`. → extract one shared test helper module.
2. **Repeated Switches** — the 16-kind cascade exists twice: `_apply`
   (events.py:330–373) and `_summary` (events.py:127–167); a 17th event type
   means edits in EVENT_TYPES plus both cascades (mild Shotgun Surgery).
   → one kind → (apply, summary) table.
3. **Data Clumps** — `(kind, stage, at, payload)` travels together through
   `_fold_line` → `_apply` → `_apply_gate`/`_apply_created` and into
   `_summary` (events.py:127, 323, 330–331, 401–402). → parsed-event value
   object.
4. **Speculative Generality** — CoreCliClient has two time seams: constructor
   `clock` (core_cli.py:137) and `status(now=…)` (core_cli.py:149–156); tests
   exercise only `now=`. → keep one seam.
5. **Primitive Obsession** — `cost_stamp: tuple[int, int]` (events.py:100) and
   `_cached: tuple[float, CliEnvelope]` (core_cli.py:143): positional tuples
   standing in for named concepts.

**Summary: 7 findings (1 documented-standard violation, 1 hint nit, 5 smells) — worst: FilesCollector's error channel is dead; an unreadable intake plane fails silently empty against the package's own degrade contract.**

## Spec (M1 work)

1. **spine.py — present, one hole.** Spec: "*single-flight provider fetch (a
   poll starts only when the previous finished)*". Overlap prevention is real
   (test_spine.py:191–219, real threads), but the slot can leak: spine.py:265–270
   claims the slot via `try_start()` then calls `run_worker` with **no exception
   guard**; if `run_worker` raises, `finish()` is unreachable and every later
   tick returns at line 266 — the spine never polls again. Probe (fake-app
   seam): `run_worker` raised → `in_flight` stayed True, 10 further ticks,
   0 provider calls. Fix is one try/except. Everything else in item 1
   verified: 1s tick (spine.py:193), multipliers 53–59, strictly-greater stale
   thresholds 68–79 (pinned test_spine.py:126–135), STALE badge at crit only
   (overview.py:150–155), hydration both sides + never re-arm
   (test_spine.py:101–120), bell-once (165–177), collector_error banner
   (overview.py:118–125), pure core importable without Textual (probe: import
   succeeds, `textual` not loaded). Exactly one frame-writer
   (`_apply_snapshot`, spine.py:309–325; `_fetch` crosses via
   `call_from_thread`, spine.py:303); `_tick_staleness` (272–286) mutates only
   staleness, on the UI thread — frames have one mutation path.
2. **shell.py — present, one wrong.** Provider via build_provider
   (shell.py:155–157), theme from tokens (37–69), zero hex literals (grep over
   all M1 files: none). But `--demo` is `store_true, default=True`
   (shell.py:183–184): demo can never be False, so `--root PATH` is dead
   (build_provider ignores root when demo=True, collectors/__init__.py:35–37).
   Same in watch.py:163–166 — M2's real collectors are unreachable from both
   CLIs despite having landed.
3. **overview.py — present.** No `clear()` anywhere; stable-key
   update/remove/append (overview.py:163–178), cursor pinned
   (test_overview_pilot.py:55–76); feed append-only, cap 2000
   (overview.py:34, 88), seq cursor 191–195. Wrinkle (spec letter kept): the
   cursor is per-followed-run — flip A→B→A resets to 0 and re-appends A's
   whole tail (overview.py:185–189); history never rewritten. Glance/services/
   RunDetail (stages+gates+cost+tail)/esc and bindings-with-descriptions all
   verified (overview.py:42–44, 62–64; shell.py:137–139).
4. **panels.py — present.** No Textual import (rich + interface.render/
   snapshot only); colors only via `token()`/`STATE_TOKENS`; "—" for missing
   cost/gates/stages (panels.py:82, 92, 210, 226, 245) — never 0.
5. **watch.py — present.** Stdlib WatchLoop + Live rotator; stall keeps last
   frame + STALE (watch.py:104–117); q/Ctrl-C → 0 (85–98); no-rich → one
   status frame, exit 0 (187–191). Observation (not spec-required):
   `_QUIT_KEYS` contains `"\x1b"` (watch.py:39) and the cbreak reader consumes
   one byte (147–151), so any arrow key's leading ESC quits the rotator.
6. **Tests — present and real.** Mutation probe: patching `Cadence.tick` to
   force every panel every frame makes the runs/glance/services assertions
   fail (60 updates each vs ~expected 30/12/4) — the counting test genuinely
   catches the bug; `calls <= ticks` pinned (test_cadence_counts.py:79); M0
   tests stay green (86 passed, incl. snapshot contract/status/demo).
7. Accepted items (clock kwarg, non-tty single frame): implemented as
   accepted, not counted.

**Summary: 4 findings — worst: the single-flight slot leaks on a failed worker dispatch; the spine then never polls again (fix: guard `run_worker`).**
