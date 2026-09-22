# Adversarial review fix ledger — M1+M2 (2026-09-22)

Reviews: `A-standards-M1-spec-M2.md`, `B-standards-M2-spec-M1.md`
(mattpocock two-axis methodology, cross-attack). Disposition of every
finding; regression tests pin each code fix. Suite after fixes: 90 passed,
ruff clean.

## Fixed

| # | Finding (source) | Fix | Pin test |
|---|---|---|---|
| A-1/B-2 | `--demo` defaulted True with no off-switch; `--root` dead in shell/watch/status — silent synthetic-data fallback against AGENTS.md rule 2 | `args.py` shared plumbing: explicit `--demo` (default off), `resolve_state_root` (explicit > `$STAMMTISCH_HOME` > conventional default); loud no-root frame | `test_collector_error_surfaces` (updated) |
| B-1 | Spine single-flight slot leaks: raising `run_worker` leaves the slot claimed forever (probe: 10 ticks, 0 provider calls) | dispatch wrapped; slot released on failure, next tick retries | `test_dispatch_failure_releases_flight_slot` |
| A-S1 | `run.cancelled` never retired running stages — cancelled run showed a live stage forever | `"cancelled"` added to the retire map → stage vocab `halted`; also fixed the map being used for membership only (raw run-state word leaked into `StageSnapshot.state`) | `test_cancelled_run_retires_its_running_stage` |
| A-S2 | Envelope tolerated `{ok:true}` with no `command` field | ok frames require a non-empty `command` string | `test_envelope_missing_command_field_is_rejected` |
| B-3 | FilesCollector error channel dead (hardcoded `None`): unreadable intake plane failed silently while events.py degrades loudly | intake plane exists-but-unreadable → loud error string into `collector_error` join | `test_intake_plane_unreadable_is_loud` |
| B-S3 | Feed cursor restart per followed run: A→B→A re-appended A's tail | per-run cursor stash dict; history never rewritten either way | covered by Pilot suite behavior (no rewrite invariant) |
| B-obs | watch quit on any arrow key's ESC byte | lone `\x1b` removed from quit keys (q / Ctrl-C only) | watch key-set unit |
| A-2 | Boundary rule wording vs composition roots | README rewritten: composition roots MAY import collectors/args; spine/screens/render may not | — |
| A-3 | README stale (no M1/M2 rows; test command failed collection without textual) | README rebuilt + `importorskip` added to the two UI test modules | collection under pytest-only env |
| A-n1 | `_AI_ENV_KEYS` dropped ANTHROPIC_API_KEY | added (+ OPENAI_API_KEY), name-only checks | files collector tests |
| A-n2 | corrupt runs sorted with the live group | `sort_runs` demotes corrupt with terminal | session sort tests |
| A-5a | `Table(box=None,…)` incantation ×3 in panels.py | `_bare_table()` helper | — |
| A-5b | `_parse_args` drift ×3 | shared `args.add_data_args` | CLI smoke |

## Accepted as-is (with rationale)

- **Dual `update_counts`** (spine dispatch counts + screen applied counts):
  deliberate cross-check — the cadence test asserts screen-applied ==
  spine-dispatch; removing one half removes the proof.
- **`CoreCliClient.status()` unused in M2**: the ×15 services lane uses
  `service_status()`; `status()` feeds M3 screens.
- **Smells deferred** (judgement calls, collector internals, churn > value
  now): 16-kind switch shape in `_apply`/`_summary`, the
  `(kind, stage, at, payload)` clump, dual clock seams in CoreCliClient,
  positional-tuple stamps, duplicated JSONL test helpers, unhinted Textual
  override stubs in overview.py. Revisit at M4 (services extraction) where
  the seams move anyway.

## Open design decisions (carried to grilling / M3)

1. `CostSnapshot` models money; cost-ledger v0 carries tokens/wall only —
   current honest stopgap `currency="tokens"`.
2. `GateSnapshot.artifact_sha256` carries the record digest; the evaluated
   artifact digest lives in `gates/<stage>.gate.json` (read it in M3?).
3. Core's manifest fold leaves `state.code` untouched on
   reconciled/resumed; our fold projects the named states (dead hosts must
   not show `running` forever) — divergence documented, revisit if the
   core changes.

---

## Grilling round (2026-09-22, report: ../../Downloads/stammtisch-tui-redesign/GRILLING.md)

**Top finding (fixed)**: the A-1/B-2 `--demo` fix had reached only status.py
and watch.py — `app/shell.py` still defaulted demo True, so the TUI silently
showed synthetic data even with `--root`. Re-applied to shell.py (parser +
`resolve_state_root` wiring + constructor default False), pinned by
`test_shell_cli_demo_is_opt_in` + `test_resolve_state_root_precedence`.

**Adjudications implemented**:
1. CostSnapshot re-typed additively: `StageUsage` rows (tokens_in/out/total,
   wall_s, None-honest — no more invented 0.0 for schema-legal nulls),
   `unit`/`token_total`/`wall_s` on CostSnapshot; money-shaped fields kept
   as token-sum aliases. Pinned in test_events_collector cost test.
2. `GateSnapshot.artifact_sha256` → `record_sha256` (the event payload
   carries the gate-record digest; the evaluated artifact digest stays in
   `gates/<stage>.gate.json` for lazy M3 detail reads, race-free because the
   file is written before the event).
3. reconciled/resumed projection KEPT and documented on RUN_STATES (core's
   manifest fold is state-neutral by enum necessity; a dead host must not
   render running forever). Engine-side gap recorded in GRILLING.md; drop
   `resumed` from `_ACTIVE_RUN_STATES` when M3 follow lands.

**Seams**: 3 defended (registry, cursor stash, boundary rule), 4 open →
folded into the M3 brief (S1 args edges, S3 cursor-under-filter, S4
corrupt-recovery policy parity with Rust read_events, S6 watch/status
renderer dedupe).

---

## C-M3 round (report: REVIEWS/C-M3.md — provisional pass; 3 fixes + pins)

| Finding | Fix | Pin |
|---|---|---|
| quant_engine copy dropped the 30s fetch ceiling → a hung provider wedged the workbench SingleFlight slot forever | `TOOL_CALL_CEILING_S` on WorkbenchScreen: every tool call runs in a one-shot pool with a hard ceiling; timeout degrades the RESULT and frees the slot (same trade-off as the old engine's manual pool lifecycle) | `test_hung_engine_call_frees_the_flight_slot` (ceiling injectable) |
| collector-error frame evicted `:detail` content with a wrong "rotated away / mistyped" diagnosis; no staleness on the screen | error frames with zero runs keep the LAST GOOD detail + say what happened; `spine_staleness` subscriber mirrors the runs-panel verdict into the border title | `test_detail_keeps_last_good_on_collector_error` |
| "demo (quantkit DOWN)" engine label lived only in the transient running line | landed results stamp `via <label>` provenance that survives | `test_engine_label_survives_on_result_surface` |
| README path table not extended for M3 | rows added for verbs/router/services/workbench/runs_detail (this commit) | — |

Suite after: 137 passed, both env modes green, ruff clean.

---

## M4 round (services out of screens — extraction, not move)

Blueprint §4 M4. The old `tui/` stays live until M5, so every
extraction is a **copy + adapt** with the old file:line cited in the
module docstring; the old tree was not touched (its own suite re-run
after this round: 523 passed, 1 skipped, 92 subtests — unchanged).
The import-graph rules are now asserted statically by
`interface/tests/test_boundaries.py` (see verdicts below).

| New module | Extracted from | What was adapted |
|---|---|---|
| `services/glance.py` | `tui/screens/dashboard.py:155-234` (`DashboardScreen._glance_tick` closure nest) | the three fetch legs (livefeed batch / ccifeed snapshot / sentiment stance) became injectable callables with offline defaults; output is a frozen `GlanceFrame` instead of formatted lines; the sparkline history bookkeeping is the pure `update_history` (48-cap, first-series trend); the `src:` provenance line is honest to its own comment (ccidx/fin-daily only when that leg served — old appended both unconditionally); a None stance score renders 0.0 instead of raising the old f-string |
| `services/decisions.py` | `tui/screens/domains.py:1144-1230` (`_decision_symbols`, `security_watchlist`) + `1661-1691` (`_load_decision`) | `state_root` is an explicit argument (no config/driver fishing; old conventional default kept); symbol normalization reuses the `quant_engine` resolver copy; `_load_decision`'s weaker defensiveness unified on the reader's shape (a malformed row is skipped, not a whole-file loss); the scan-top thesis card keeps decide.py's Chinese format verbatim |
| `services/board_merge.py` | `tui/screens/domains.py:537-589` (`_apply_adapters` / `_apply_quotes` — the futures 3-leg merge) | frozen `BoardRow` model (fresh containers every merge); the protocol documented as a 4-clause contract (arbitrary leg order, late legs never wipe standing rows, pending placeholders survive, the quote pass updates in place); the screen's two dict caches collapse into one standing-row mapping; an unchanged error keeps row identity for the old repaint suppression; quote-pass `recent`/`curve` coerce to tuples to stay typed |
| `services/intake_format.py` | `tui/screens/daily_intake.py:254-414` (`_format_result`, `_artifact_document`, `_canonical_records`, `_evidence_exceptions`) | output is a frozen `IntakeReportView` (rows, not one formatted string; the old prose survives as `notes`); the duck-typed `result` access is kept so the old-tree `IntakeResult` and test doubles both fit; every defensive `isinstance` clause kept 1:1 (malformed artifacts degrade to named error fields, counts stay `"?"`-honest) |
| `services/feeds.py` + `collectors/feeds.py` | `tui/livefeed.py:27-37` (`fetch_batch` contract) + the provider chain it delegated to (`datafeeds/providers/tencent.py`, `yahoo.py`, `datafeeds/service.py:19-43`) | closes the M2 gap (quotes were demo-only): stdlib-only provider copy (Tencent GBK batch + Yahoo chart fallback, per-row `source` stamps); the datafeeds proxy/tracking/cache layers are deliberately NOT copied (they move with the old tree at M5); `collectors/feeds.py` adds the TTL batch (30s, the old glance cadence), per-symbol age normalization (CN `YYYYMMDDHHMMSS` / dashed / slash / Yahoo epoch), and keep-last-good STALE semantics (a failed refresh ages the last rows toward the F flag); demo path untouched; fetch injectable — tests run offline |

Boundary verdicts (`tests/test_boundaries.py`, AST walk over all of
`interface/`, loud file:line failures; negative self-check verified):

- (a) zero `tui/` imports anywhere under `interface/` — enforced;
- (b) `services/` imports no `interface.app|screens|render|collectors`
  and no Textual — enforced;
- (c) `screens/` + `app/spine.py` restricted to `snapshot`, `render`,
  `app.verbs`, `app.router`, `app.spine`, `services` (the workbench
  seam) — composition roots exempt by the README rule — enforced;
- (d) `render/panels.py` has zero Textual imports — enforced.

Deliberately deferred to M5: wiring `GlanceService` into the overview
(the overview keeps its current demo glance from the snapshot — the
real legs land with the glance screen), DecisionReader / board_merge
consumers (no SECURITY/futures screens exist yet in the new tree), the
intake formatter screen, and the ccifeed/sentiment legs of glance.

Dead-code sweep (C): interface/ came back clean — the only unreferenced
top-level name was `status.py`'s `_SECTIONS` tuple (section headers are
built inline below it); removed. No TypingText-class leftovers exist in
this tree. The WATCH interactive upgrade stays M5, untouched.

Suite after M4: 188 passed (137 → 188), ruff clean (ruff 0.16.8
defaults — which since this release include the I/UP/SIM-class rules —
plus E501 at line length 100).

---

## M5 round (finish line: watch interaction, the first write path, retirement)

| Change | Pin |
|---|---|
| watch keys: `p` pauses AUTO-rotation while polling/staleness/manual keys continue (a PAUSED stall still shows the STALE banner); digits `1..9` jump pages (out-of-range ignored); space still advances; every manual page change and every pause toggle reset the rotation window | `test_watch.py` (pause across two rotate windows with a tiny `rotate_s` + injected clock, digit jump, paused stall → STALE, quit-key set = q/Ctrl-C only) |
| watch bug found by those pins: the FIRST auto-rotation fired after one interval, not a full window (`_last_rotate` initialized to 0.0 against `time.time()`-scale clocks) — the window is now measured from loop start | digit/unpause tests (no rotation before a full window) |
| `core_cli.delete(run_id)` implemented — the sole whitelisted WRITE op (op "delete", args `[run_id]`): never TTL-cached, never budget-gated (its spawns still count), envelope decides, `data.removed` echoes into the audit line; whitelist = {status, delete} with init/run still raising | `test_core_cli.py` write-op section (exact spawn argv, status TTL/budget accounting unchanged around deletes, empty id and missing binary fail closed without spawning) |
| spine: `force_refresh()` — one immediate full-panel poll after an audited write, QUEUED behind an in-flight poll so single flight holds | `test_spine.py` forced-refresh section |
| router: `:delete <run-id>` joins the one vocabulary — the id is validated against the last frame BEFORE any dialog (unknown id → notify, nothing pushed, never recorded) | `test_router.py` delete section |
| `app/confirm.py`: the fail-closed ConfirmDialog — Cancel focused default, left/right cycle, Esc AND 30s of silence resolve Cancel (any key re-arms), resolution single-shot, the dialog itself performs no write | `test_delete_pilot.py` |
| shell: the audited delete lane — confirm runs the core delete OFF the UI thread behind its own SingleFlight slot, lands as notify + `[audit]` activity-feed lines (`ui.delete confirmed/cancelled run=<id>`, then the core verdict `core.delete ok run=<id> removed=True` / `failed`/`error`/`refused`), forces one spine refresh; demo/no-root confirms refuse honestly instead of faking a delete | `test_delete_pilot.py` (confirm path against the extended FAKE core binary, core-error and raising-worker paths, second-delete-while-in-flight refusal) |
| fake_core.py: `delete` mode — records the call, `FAKE_CORE_DELETE_FAIL` forces an ok:false envelope | `test_core_cli.py` fixture tests |

`REVIEWS/RETIREMENT.md` records the honest M5 verdict: retirement NOT
yet safe (6 of 35 old screens matched outright, 20 absent). Committed
path: Option B — `tui/` stays the deep-feature app, `interface/` is the
monitoring spine, with a per-screen absorption trigger. The English-only
decision is documented there too (AGENTS.md rule 7 overrides the
blueprint's i18n item for `interface/`; the old tree's zh chrome is
grandfathered until retirement).

Suite after M5: 221 passed (188 → 221), both env modes green
(pytest-only env: 162 passed, 8 skipped), ruff clean.

---

## M6 round (Option B, first absorption wave: five read-only screens)

Per `REVIEWS/RETIREMENT.md` Option B (absorb an old screen only when
its monitoring value outranks its write risk). The five chosen:
FeedHealthScreen (feeds.py:18), LedgerScreen (ledger.py:27 + the
portfolio.py FIFO fold), EnergyScreen (energy.py:579),
PolymarketScreen (polymarket.py:338), SentimentScreen (brief.py:232 +
tape.py desk_sentiment + the history index). The old tree was not
touched; every extraction is copy+adapt with the old file:line cited in
the module docstring.

| New module | Extracted from | What was adapted |
|---|---|---|
| `services/feeds_health.py` | `tui/datafeeds/registry.py:24-103` (`ProviderStats`/`tracked`/`all_stats`) + `cache.py:107-113` (`cache_stats`) | frozen `ProviderHealth` rows (updates re-read under the lock — no lost increments); the interface's own feeds lane reports into it (`services/feeds.py` wraps the tencent/yahoo calls in `tracked`; `collectors/feeds.py` reports its keep-last-good fresh/stale split — the old FEEDS cache line, honestly mapped to what this tree actually has); latency bands (warn 1500 ms / crit 4000 ms) shared like every other semantic threshold |
| `services/ledger.py` | `tui/portfolio.py:26-138` (read + FIFO fold) + `tui/screens/ledger.py:78-92` (the quote step) | READ ONLY (the old `add_fill`/`remove_fill` stay in the old tree — write ops need the audited confirm lane); `None` state root reads as no ledger (no conventional-default fishing); the mark pass is an injectable quote fn (default: the services feeds lane, lazily); a missing mark stays `None` → the screen renders `—` |
| `services/energy.py` | `tui/energy.py:36-576` (series table, pinned-proxy egress, URL build, row collapse, fetch, formatters) | frozen `SeriesRow`/`EnergyFrame`; the I/O seam is an injectable `transport` callable (injection over monkeypatching); `build_url`/`parse_rows` take `today` for deterministic forecast windows; `date.today()` → UTC `_today()`; NaN guard via `math.isnan` (ruff PLR0124) |
| `services/polymarket.py` | `tui/polymarket.py:32-327` (Gamma fetch, market collapse, pinned-proxy egress, formatters) | frozen `MarketRow`/`MarketTape`; injectable transport; the `/`-style LOCAL filter stays screen-side (view state, not data) |
| `services/sentiment.py` | `tui/brief.py:17-220` (report load) + `tui/tape.py:14-554` (scorer + formatter) + `tui/history.py` (adapted) | every path is an explicit argument (tests use tmp fixtures only — no real workspace reads); the history index drops the old SQLite `HistoryStore` for a direct fail-closed scan of the same two artifact shapes (intake outranks legacy, same ranking rule); the per-symbol overlay (alias/needle machinery + the old offline symbol resolver) deliberately NOT copied — it belongs to the future domain screens |

Screens (`screens/{feeds_health,ledger,energy,polymarket,sentiment}.py`)
mount through the router (`:feeds` etc., palette vocabulary +
descriptions, recents for free). feeds/energy/polymarket are
**spine-live**: they poll on the spine's `services`-due (×15) behind a
new `app.spine.SlowLane` — the spine's single-flight discipline applied
to a screen's own service calls (never-overlap, generation-based
drop-stale, raising work degrades to `{"ok": False, ...}`) — and mirror
the spine's services staleness into their border badge. ledger and
sentiment are **one-shot** (`r` re-reads; a daily-written file and a
daily report have nothing to poll). Every screen: esc pops, j/k move
the cursor, every binding described.

Bugs found and fixed while pinning the wave:

- slow-lane repaints cleared the DataTable and reset the cursor — the
  exact drift the overview wall's keyed-sync exists to prevent. Every
  tape screen now re-anchors its cursor across repaints (and polymarket
  across filter changes); pinned by the j/k Pilot assertions.
- polymarket re-deliveries stole focus back from the filter input;
  the table now auto-focuses only on the FIRST landed tape.
- sentiment ←/→ initially walked the newest-first entry list backwards
  (← hit "Oldest report." at the NEWEST day); ← now walks toward older
  days, and the day counter reads oldest=1.
- the arrows also needed `priority=True` — the scrollable body consumes
  them otherwise (the old screen did the same).

Boundary growth (`tests/test_boundaries.py`): rule (e) — the five M6
screens carry ZERO module-level `interface.services` imports (injection
only; transports resolve lazily in function bodies) — plus a named
regression that the five M6 service modules stay `tui/`-free and
Textual-free. Latency marking reuses the existing `state.*` tokens; no
new hex.

Suite after M6: 302 passed (221 → 302), ruff clean (ruff 0.16.8).

---

## D-M6 round (adversarial review D — findings 1-3, 4-partial, 7; spec-axis mirror)

Report: `REVIEWS/D-M6-standards.md`. Every finding dispositioned;
each code fix carries a pin.

| Finding (source) | Fix | Pin |
|---|---|---|
| D-1: the self-reported M6 UI fixes (cursor re-anchoring, polymarket focus latch, spine-live re-poll wiring) were real in code but pinned by no shipped test — README:121 states them as contract | new `tests/test_m6_pins.py`: pilots that deliver a SECOND spine frame (the ×15 services-due wiring itself, no manual refresh) through a gate that pins WHICH data frame 2 carries, then assert the cursor re-anchors to the same row key in all three spine-live screens with frame-2 data genuinely different (changed value + appended row / bumped counter), and that a re-delivery never steals focus from the polymarket filter (`_autofocused` latch) | `test_feeds_cursor_survives_second_spine_frame` / `test_energy_cursor_survives_second_spine_frame` / `test_polymarket_cursor_survives_second_spine_frame` / `test_polymarket_redelivery_never_steals_filter_focus` |
| D-2 (spec mirror): `SingleFlight.try_start` (UI thread) vs `finish()` (worker threads) was a lock-free check-then-set — not atomic | `app/spine.py`: one `threading.Lock` guards `try_start`, `finish`, and the `in_flight` read | `test_single_flight_claim_is_thread_safe` (8-thread barrier hammer ×3 rounds: exactly one winner; finish from another thread re-opens exactly one slot) |
| D-3: rule (e) walker inspected only direct `tree.body` children, so a banned `interface.services` import under a top-level `if`/`try` escaped; its docstring claimed "ONLY direct children of the module body execute at import time" (false) | `test_boundaries.py`: `_import_time_services_imports` walks the whole module top level including `if`/`try` bodies (`if TYPE_CHECKING` bodies and function/class bodies stay the sanctioned seams; relative imports resolved); docstrings corrected | `test_rule_e_walker_flags_guarded_module_level_imports` (synthetic in-test AST: try-guarded and if-guarded banned imports MUST flag, TYPE_CHECKING and lazy function-body imports MUST NOT — nothing violating is written into the tree) |
| D-4: ~110 lines of pinned-proxy egress machinery near-verbatim across `services/energy.py` + `services/polymarket.py`, self-admitted "kept in sync" | new shared `services/egress.py` (`as_http_proxy` / `Egress` / `resolve_egress(env_var=…)` / `PinnedProxyHandler` / `open_via_pinned_proxy(user_agent=…)` / `proxy_request_error`); both twins rewritten to thin wrappers (product env-var + UA as the only parameters); services-importing-services is sanctioned (rule (e) governs SCREEN modules) | twin service tests green untouched (`test_services_energy.py`, `test_services_polymarket.py`) + new `test_services_egress.py` (env-var isolation, explicit-arg precedence, handler pin/reject, pre-I/O validation, stable error copy) |
| D-5 (partial): the staleness-badge block triplicated across the three spine-live screens (only widget id + title differed) | new `render/stale.py::stale_badged_title(base, level)` — Rich-only, closed vocabulary, tokens only (`stale.warn` / `stale.crit`, no new hex); all three `spine_staleness` handlers reduced to helper + border set. (The `action_refresh` guard and j/k cursor actions stay per-screen: the review itself judged full parameterization overreach) | `test_stale_badged_title_levels` + the badge-mirrors-into-border assertions inside each second-frame pilot (`_assert_badge_mirrors_into_border`) |
| D-7 (minor): `services/polymarket.py::market_url` exported and tested but read by no screen — leftover from the removed browser launch | removed (`market_url` + `MARKET_BASE`); grep confirmed the only readers were its own test and the untouched old `tui/` tree (which keeps its own copy) | `test_format_detail` (trimmed; the URL assertions died with the helper) |

Not reproduced / nothing to fix: D-5's "honest reuse" (SlowLane composes
SingleFlight — no duplication), D-6's verified claims (injectable quote
fn, sentiment direction, polymarket Enter, tokens, no credentials) —
the review itself marked them verified. D-4's "quintuplicated"
refresh-guard/cursor actions were deliberately NOT extracted (review:
"full parameterization would be overreach").

Suite after D-M6: 315 passed (302 → 315; +13 new tests: 5 M6 pins,
1 thread-hammer, 1 walker self-check, 6 egress units; the trimmed
`market_url` assertions folded into the renamed `test_format_detail`),
lite env 235 passed + 14 skipped (227+13 before), ruff clean
(ruff 0.16.8, line 100).

### Flake watch

One full-suite run under concurrent load showed a single failure (rerun
green twice, 315/315; the known timing-marginal workbench off-thread
test is the prime suspect — already widened once in the M5 round).
Monitor; widen again only on recurrence with a captured failure name.
