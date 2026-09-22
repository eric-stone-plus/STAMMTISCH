# Old-tui retirement assessment — M5 (2026-09-22)

An honest accounting, not a victory lap. The question M5 was asked to
answer: **can `tui/` be retired now that `interface/` exists?** The
answer is **no** — and this document shows the arithmetic, names what is
deliberately NOT covered, and commits to one of the two possible paths
forward instead of dodging.

Sources: the pre-rebuild read-only inventory of `tui/` (the redesign
research document `stammtisch-tui-inventory.md`, §2 — 35 screens; kept
outside this repository with the other redesign notes), and the current
`interface/` tree (suite: 221 passed in the full env, 162 passed +
8 skipped in the pytest-only env; ruff clean). The old tree was NOT
modified; nothing here imports it (enforced statically by
`tests/test_boundaries.py`).

## 1. Coverage matrix — all 35 old screens vs interface/ today

Legend: **COVERED** = a screen of equal or better scope exists;
**REPLACED** = old screen superseded by a strictly better mechanism;
**PARTIAL** = a screen exists but with named gaps; **LOGIC-ONLY** = the
business logic was extracted into `services/` (M4) but no screen renders
it yet; **NOT COVERED** = nothing in `interface/` touches it.

| # | Old screen (file:line) | interface/ today | Verdict |
|---|---|---|---|
| 1 | DashboardScreen (dashboard.py:71) | OverviewScreen wall: runs registry table + verbs (`/ n N f s c v`, cursor discipline), activity feed, glance panel, services strip, banner flags; `:delete` gives the registry-delete write (single-run, audited — no multi-select, see §4) | PARTIAL — no intake rows in the registry (intake lives in the status tier + C flag), no quick-start nav, no plugins view, no SystemHud, no config resync |
| 2 | PipelineRunScreen (runs.py:34) | — | NOT COVERED |
| 3 | RunInspectorScreen (runs.py:330) | `:detail <run-id>` RunsDetailScreen: streaming stages/gates (record digests)/cost usage rows/full event tail, corrupt-run honesty | PARTIAL — no receipts view, no export/verify, no session summary |
| 4 | ValidateScreen (runs.py:630) | — | NOT COVERED |
| 5 | PipelineViewScreen (runs.py:671) | — | NOT COVERED |
| 6 | ChatScreen (chat.py:96) | — | NOT COVERED |
| 7 | AskSessionScreen (chat.py:444) | — | NOT COVERED |
| 8 | ConfigScreen (config_screen.py:25) | — | NOT COVERED |
| 9 | DailyIntakeScreen (daily_intake.py:22) | intake sessions in the snapshot (status tier rows + C flag); `services/intake_format.py` extracted the result formatter | PARTIAL/LOGIC-ONLY — no capture screen, no live progress, no GALAHAD analyze |
| 10 | DomainBrowserScreen (domains.py:25) | — | NOT COVERED |
| 11 | FuturesScreen (domains.py:145) | `services/board_merge.py` (3-leg merge) + the feeds lane | LOGIC-ONLY — no screen |
| 12 | ShippingScreen (domains.py:721) | — | NOT COVERED |
| 13 | SecurityScreen (domains.py:1269) | `services/decisions.py` (decisions reader + watchlist merge) | LOGIC-ONLY — no screen |
| 14 | StrategyScanScreen (domains.py:1930) | — | NOT COVERED |
| 15 | FeedHealthScreen (feeds.py:18) | services strip (core/quantkit/AI availability) + spine staleness everywhere | PARTIAL — no per-provider ok/fail/latency counters, no cache stats, no quote tape |
| 16 | LedgerScreen (ledger.py:27) | — | NOT COVERED |
| 17 | BrokerScreen (broker.py:46) | — | NOT COVERED |
| 18 | CryptoBoardScreen (crypto_board.py:44) | — | NOT COVERED |
| 19 | ConfirmScreen (modals.py:18) | `app/confirm.py`: fail-closed confirm (Cancel default, esc/silence never action, both outcomes audited) | REPLACED (better) |
| 20 | SymbolInputScreen (modals.py:111) | command bar/palette input + workbench form fields | PARTIAL — no free-form symbol-input modal contract |
| 21 | KeyHelpScreen (modals.py:147) | Textual Footer (every binding carries a description) | PARTIAL — no generated per-screen key sheet |
| 22 | HelpScreen (modals.py:178) | — | NOT COVERED |
| 23 | DataFetchScreen (analysis.py:114) | workbench `fetch` tool | COVERED |
| 24 | BacktestScreen (analysis.py:199) | workbench `backtest` tool | COVERED |
| 25 | IndicatorsScreen (analysis.py:301) | workbench `indicators` tool | COVERED |
| 26 | PortfolioScreen (analysis.py:381) | workbench `portfolio` tool | COVERED |
| 27 | GatesScreen (analysis.py:471) | workbench `gates` tool | COVERED |
| 28 | TerminalChartScreen (charts.py:186) | — | NOT COVERED |
| 29 | CrawlerPanelScreen (crawlers.py:191) | — | NOT COVERED |
| 30 | EnergyScreen (energy.py:579) | — | NOT COVERED |
| 31 | PolymarketScreen (polymarket.py:338) | — | NOT COVERED |
| 32 | CryptoScreen (polymarket.py:462) | — | NOT COVERED |
| 33 | RacingScreen (racing.py:71) | — | NOT COVERED |
| 34 | CasinoScreen (racing.py:258) | — | NOT COVERED |
| 35 | SentimentScreen (brief.py:232) | sentiment stance is one glance leg (`services/glance.py`, offline default) | PARTIAL/LOGIC-ONLY — no tape screen, no history navigation |

Tally: 5 COVERED + 1 REPLACED + 9 PARTIAL/LOGIC-ONLY + 20 NOT COVERED
= 35. **Six of 35 old screens are matched or superseded outright; nine
exist only partially or as extracted logic; twenty are absent.**
Retirement deletes features operators use.

### Non-screen assets the old tree also hosts (no interface/ equivalent)

chat transport (`ai_driver.py`), the intake contract validator +
IntakeSupervisor/IntakeSteward (`intake.py`, `intake_job.py`,
`intake_steward.py`), the stdlib chart server + vendored web charts
(`chart_server.py`, `web_chart.html`, `static/`), validated-bars store,
`brokers/` (paper-gated trading), the full `datafeeds/` registry/
cache/journal stack (interface/ carries only the provider copy +
TTL batch in `collectors/feeds.py`), crawler operations (podman/systemd/
sources.conf), the offline symbol resolver, and i18n (`lang.py`).

## 2. What interface/ has that tui/ never had

For fairness, the ledger runs both ways: one refresh spine with per-panel
cadence + STALE-as-data + single flight (the old tree had eight
unsynchronized clocks); one color truth; one command vocabulary behind
the router; a services lane with enforced boundaries; headless tiers
(status/watch) and pipe-safe output; and — new at M5 — the workstation's
first **audited, fail-closed write path** (`:delete`) with an activity
feed trail. None of that speeds up retirement by itself; it is the
foundation the remaining screens would land on.

## 3. Verdict and the path forward

**Retiring `tui/` today is NOT safe.** The gap is not the five covered
workbench tools or the monitoring wall — it is the deep-feature half of
the product: chat, config editing, the three domain boards and their
scans, intake capture, brokers, ledger, crawlers, the specialty tapes,
and the chart server.

Two options, with costs:

**Option A — absorb the remaining screens onto the router (full
replacement).** Keep `tui/` live while M6+ milestones port surfaces in
dependency order: board services first (futures/shipping/security data
legs are half-extracted already), then their screens, then chat (move
`ai_driver.py` behind a service seam), config (a `ConfigService.apply`
with hot-apply semantics), intake capture (supervisor + screen), then
the long tail (ledger/brokers behind the audited-confirm pattern `:delete`
established, crawlers, tapes, chart-server entry). Costs: several
milestones of work; every additional write path (orders, fills, config,
crawlers) multiplies the audit surface that must be designed, pinned and
reviewed; dual-tree drift grows with every absorbed surface until the
final cutover; the biggest single risk is domains-grade data merging
(2,146 LOC of the old tree) landing on the new wall without regressions.

**Option B — keep `tui/` as the deep-feature app; `interface/` is the
monitoring spine.** Declare the split permanent-ish: `interface/` owns
monitoring (wall, watch, status, run detail, workbench reads, the one
audited delete), `tui/` owns depth (chat, boards, capture, trading).
Costs: two apps to maintain forever; run state is presented twice (both
read the same events fold and core CLI, so they cannot disagree about
facts, but features must be fixed twice); the old tree's zh chrome and
its fragility ledger (inventory §5) stay alive; contributors must always
know which tree a change belongs in.

**Committed recommendation: Option B now, with a per-screen absorption
trigger.** The remaining gap is dominated by surfaces whose monitoring
value is low and whose write/blast radius is high (brokers, crawlers,
config hot-apply) — exactly the surfaces a read-first, audited spine
should NOT absorb cheaply. Keep the split; absorb a specific old screen
onto the router only when its monitoring value outranks its write risk
and its service extraction already exists (the honest near-term
candidates: a feeds-health screen over the existing feeds lane, and
receipts/export on `:detail`). Re-assess retirement per absorbed screen,
not on a calendar.

## 4. Registry CRUD scope note (the M5 write path)

The old dashboard's registry delete included multi-select batches and
extend flows. `interface/`'s `:delete` is deliberately SINGLE-run:
one id, one confirm dialog, one audited worker, one forced refresh.
Batch delete waits until a genuine batch write op exists in the core
CLI contract (see README's deferral note) — a confirm loop over N
single deletes is not a batch op, it is N chances to be wrong.

## 5. The English-only decision (recorded, not open)

The redesign blueprint carried an i18n item (en/zh relabeling, from the
old `lang.py`). For `interface/` it is **resolved: no i18n layer.**
AGENTS.md rule 7 ("framework files and UI copy are English") governs
this repository and overrides the blueprint for the new tree — every
interface/ string is English-only, and `tests/test_boundaries.py` keeps
the tree free of old-tree imports including its i18n machinery. The old
`tui/`'s Chinese chrome (and its mixed-language drift, inventory §5.11)
is **grandfathered until retirement**: under Option B it stays as-is in
the deep-feature app; if an absorbed screen ever carries operator-facing
Chinese content (e.g. the decision thesis card format preserved in
`services/decisions.py`), that content is data, not chrome, and may
preserve the text it exercises per rule 7's fixture carve-out.
