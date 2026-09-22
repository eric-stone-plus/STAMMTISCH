# C-M3 — adversarial review (standards / spec / seams)

Method: matt-pocock two-axis + seam grilling; read-only probes (purity, binding
maps, Pilot stack/orphan/error-frame runs). Suite: 134 passed (baseline 92
subsumed); `uvx ruff@0.16.8 check interface/` clean; no line >100; no hex
outside `render/tokens.py`; no `tui/` import (docstring mentions only);
English throughout (box-drawing separators are repo convention).

## Standards

1. **README drift — the standards source contradicts the code it governs.**
   `interface/README.md:10-22` still lists `app/` as "spine.py, shell.py" and
   `screens/` as "overview.py" only; `app/verbs.py`, `app/router.py`,
   `screens/workbench.py`, `screens/runs_detail.py`, `services/` are absent,
   and the boundary-rule paragraph (README.md:24-31) was never extended for the
   services seam — only `services/__init__.py:3-7` claims that role. FIXES.md
   A-3 established README-rebuild-per-milestone as the norm.
2. **Mysterious Name:** `RunsDetailScreen` (screens/runs_detail.py:33) vs
   `RunDetailScreen` (screens/overview.py:52) — near-identical names, different
   tiers (streaming screen vs M1 modal); grep finds both.
3. **Type hints loosened at the new seams:** `Recents.__iter__ -> Any`
   (app/router.py:145-146), `ScreenFactory = Callable[..., Any]`
   (app/router.py:59); workbench renderers lack return annotations
   (screens/workbench.py:194, 208, 224, 247, 264) where panels.py annotates
   `-> RenderableType`.
4. **Duplicated Code (tests):** `_harness` copied verbatim in
   tests/test_verbs.py:175, tests/test_router.py:185,
   tests/test_workbench_pilot.py:28.
5. **Inconsistent recents policy:** router "Unknown or failed commands … NEVER
   recorded" (app/router.py:11-12), but the workbench records the input line
   before validation/run (screens/workbench.py:385-389) — `fast=abc` failures
   persist in MRU.
6. Smells checked and passed: no Feature Envy/Message Chains/Middle Man;
   `WORKBENCH_TOOLS`/`TOOLS` drift guarded by assert (screens/workbench.py:101).

Worst: README boundary rule and path table not extended for M3 (finding 1).

## Spec

1. **verbs — present.** "`/` regex filter … case-insensitive over
   id+pipeline+state+flags; invalid regex keeps old filter + notify; hidden
   rows stay hidden across ×2 refresh; empty/esc clears; hint in border
   title" — app/verbs.py:87-109, screens/overview.py:352-365, 236-249;
   Pilot-pinned (tests/test_verbs.py:193-268). `n`/`N` wraparound + no-filter
   notify (verbs.py:192-211, overview.py:367-383); `f` latch row-key riding,
   hidden survival, no clamped re-latch (verbs.py:155-190, tests/test_verbs.py
   :308-362); `s` snapshot-side cycle, ONE keyed rebuild (verbs.py:117-132,
   overview.py:398-403); `c` masked snapshot-only line via copy_to_clipboard
   (panels.py:320-338, overview.py:405-413); `v` cursor-first modal
   (overview.py:415-423). Every binding described (overview.py:112-122).
2. **router — present.** Lazy registry, `goto(name, **kwargs)`, push/pop with
   no `switch_screen` anywhere (docstring note only, router.py:1-7); `:`/⌃P
   share `run_command` (router.py:270-274); MRU deque(8) (router.py:56,
   127-152); unknown/failed never recorded (router.py:196-239, tests pin).
3. **workbench — present, one honesty wrinkle.** ONE spec-driven screen for
   the five tools (screens/workbench.py:67-101), worker thread + SingleFlight
   never-overlap (377-402, test pins one engine call), honest degraded
   resolution (services/quant_engine.py:451-468), in-memory recents, no tui
   imports. Wrinkle: the degraded label ("demo (quantkit DOWN)") shows only in
   the transient "running…" line (workbench.py:396-398) — once the result
   lands, nothing on screen says it is demo output. "honest degraded
   rendering when quantkit absent" is only transiently honest.
4. **runs_detail — present.** stages flow (panels.py:392-402), gates with
   record_sha256 (panels.py:405-437), cost block unit/token_total/wall_s/usage
   with "—" nulls (panels.py:355-389), full tail, corrupt error prominent
   (panels.py:440-471); tests pin including the corrupt case.
5. **Seam pins S1/S3/S4/S6 — present** (args.py:31-47 + tests/test_status.py
   :91-128; test_verbs.py:308; events.py:22-33 + test_events_collector.py:422
   -453; watch.py:181-186 + test_status.py:134-148).
6. "snapshot.py untouched (claimed)": unverifiable by git (whole `interface/`
   is untracked); FIXES.md:64-66 corroborates the record_sha256/unit fields
   predate M3. "UI imports only snapshot/render/router/verbs (+services)" —
   holds (import-graph grep; spine import in workbench is UI-internal).

Worst: the degraded-engine label vanishes when the result lands (item 3).

## Seams

Defended (probe/citation): verbs purity — no `textual` in sys.modules after
import (imports at verbs.py:28-34); filter vs ×2 reconciliation pinned by
tests/test_verbs.py:193-222; ctrl+p — `ENABLE_COMMAND_PALETTE=False` +
single custom binding (shell.py:154-159; binding map probed: `ctrl+p ->
palette` only); services/ imports only snapshot (quant_engine.py:26) — no
reverse dependency; workbench single-flight drops (not queues) a second
submit with notify (test_workbench_pilot.py:158-187); leaving mid-run orphans
nothing — worker finishes, delivery dies on `is_mounted`
(workbench.py:413-427; probe: results_landed=0, flight finished, app alive);
copy drift audited — compute_indicators/evaluate_gates match tui/engine.py
:296-317/:437-471, dropped series fields are unused by renderers.

Open:
1. **No engine-call timeout — a hung fetch wedges the tool forever.** The copy
   dropped tui/engine.py:188-243's 30s per-call ceiling ("prevents a hung
   provider from blocking the entire zone scan"); quant_engine.py:147-150
   calls `fetch_ohlcv` bare, and SingleFlight (workbench.py:377-380) has no
   timeout, so one hung network call leaves the tool notifying "already in
   flight" until restart. The classic copy-drift bug, liveness flavor.
2. **Collector-error frame evicts the detail body with a wrong diagnosis.**
   Probe: provider raising while `:detail` is open → body becomes "run X is
   not in the current frame (finished and rotated away, or the id was
   mistyped)" + notify "left the current frame" (runs_detail.py:84-94) — the
   run did not rotate away; the collector died. No `spine_staleness` on the
   screen, so nothing says STALE either. Also re-renders every tick (feed
   multiplier 1 rides the `{"runs","feed"} & due` gate, runs_detail.py:80).
3. **Modal binding chain blocks the command surface:** under any ModalScreen,
   q/⌃P/`:` are dead (Textual `_modal_binding_chain` ignores everything
   before the last modal; probed). Defensible, but it is Textual's behavior
   being relied on implicitly, not a documented decision.

Worst: the un-timed engine call wedging the workbench tool slot (item 1).

## Ranked minimal fixes

1. quant_engine: reinstate a per-call ceiling (30s futures, as tui/engine.py)
   so a hung provider cannot wedge the SingleFlight slot permanently.
2. runs_detail: on `collector_error` frames keep the last-known body + a
   STALE line; show the "not in the current frame" text only for healthy
   frames.
3. workbench: persist the engine label into the RESULT border title after
   landing.
4. README: add the five new paths and extend the boundary rule with the
   services seam.
5. Rename `RunDetailScreen` → `RunDetailModalScreen` (disambiguate).
6. Minor: renderer return annotations; `Recents.__iter__` typing; shared test
   harness; workbench recents only on accepted runs.

Verdict: provisional pass — no spec item missing; fix 1-2 before M4 builds on
the workbench lane.
