# STAMMTISCH interface

The rebuilt terminal workstation, staged M0..M5 per the redesign blueprint.
This package is the UI; it owns no business logic.

| Path | Purpose |
|---|---|
| `snapshot.py` | Frozen dataclass contract — the ONLY interface between collectors and UI tiers |
| `args.py` | Shared `--demo` / `--root` CLI plumbing (explicit flags; root resolves explicit > `$STAMMTISCH_HOME` > `~/.local/share/stammtisch`) |
| `collectors/` | Read-only data layer plus the ONE audited write: deterministic demo; real collectors (events.jsonl byte-offset tail + fold, intake/cost files, core-CLI adapter with TTL + strict op whitelist + the sole `delete` write op) behind long-lived sessions |
| `render/` | Pure snapshot → Rich renderables; `tokens.py` is the single color truth; `flags.py` fixed-width letter flags |
| `app/` | `spine.py` (tick/multipliers/STALE/single-flight + `force_refresh` + the M6 `SlowLane` bounded screen-poll lane), `shell.py` (thin Textual app + the audited delete lane), `verbs.py` (filter/sort/follow/cursor policy), `router.py` (screen registry, `:` bar + ⌃P palette, one command vocabulary, recents), `confirm.py` (the fail-closed confirm dialog for the ONE write) |
| `screens/` | `overview.py` (wall + verb bindings + audit chrome), `workbench.py` (spec-driven five quant tools), `runs_detail.py` (`:detail <id>` full run screen), plus the M6 absorbed read-only screens: `feeds_health.py` (`:feeds`), `ledger.py` (`:ledger`), `energy.py` (`:energy`), `polymarket.py` (`:polymarket`), `sentiment.py` (`:sentiment`) |
| `services/` | UI-free service lane, each module a copy+adapt extraction citing its old file:line (no `tui/` imports): `quant_engine.py` (engine call shapes, quantkit or demo — tui/engine.py contract), `glance.py` (glance composition — dashboard.py:155-234), `decisions.py` (decisions/latest.json reader + watchlist merge — domains.py:1144-1230, 1661-1691), `board_merge.py` (the 3-leg row-merge protocol — domains.py:537-589), `intake_format.py` (intake result → render-ready view — daily_intake.py:254-414), `feeds.py` (livefeed `fetch_batch` contract — tui/livefeed.py + datafeeds provider chain), and the M6 extractions: `feeds_health.py` (provider counters + lane cache — tui/datafeeds/registry.py + cache.py), `ledger.py` (fill read + FIFO fold + marks — tui/portfolio.py + screens/ledger.py), `energy.py` (EIA v2 watchlist — tui/energy.py), `polymarket.py` (Gamma tape — tui/polymarket.py), `sentiment.py` (report load + tape scorer + history index — tui/brief.py + tape.py + history.py), plus the shared `egress.py` pinned-proxy contract (extracted from the energy/polymarket twins by review D) |
| `status.py` / `watch.py` | Tier 1 one-shot summary (stdlib) / tier 2 Rich Live rotator with keys: `p` pause, `1..9` page jump, space advance, `q`/Ctrl-C quit |
| `REVIEWS/` | Adversarial review reports, the fix ledger, and `RETIREMENT.md` (the old-tui retirement assessment) |
| `tests/` | Contract, collector, spine, Pilot, watch-key, and cadence-counting tests |

## Boundary rule

UI modules (`app/spine.py`, `screens/`, `render/`) import only the
`snapshot` contract, the `SnapshotProvider` protocol, and `render` —
plus, since M3/M4, `app.verbs` / `app.router` / `app/spine`
(SingleFlight) and the `services/` lane (the workbench seam). The
`services/` lane imports only the snapshot contract + stdlib (no UI
types, no reverse dependencies). Composition roots — `app/shell.py:main`,
`watch.py:main`, `status.py:main` — additionally import `collectors` and
`args` to build the provider and resolve the root; that is their job;
collectors may consume `services/` (the feeds lane does). No tier ever
falls back to synthetic data silently: `--demo` is explicit, a missing
root is a loud `collector_error` frame.

Since M6 the five absorbed screens (feeds_health / ledger / energy /
polymarket / sentiment) additionally reach their services by
**injection only**: they carry zero module-level `interface.services`
imports — the transport functions resolve lazily inside function bodies
at call time, so importing a screen can never drag a network or file
transport onto the import path (the M3 workbench seam stays exempt:
`resolve_engine` is pure dispatch).

All of this is asserted statically by `tests/test_boundaries.py`, which
walks the AST of every module under `interface/`: zero `tui/` imports
anywhere, no service → UI/collector reverse dependency, screens/spine
restricted to the sanctioned set, `render/panels.py` Textual-free, and
the M6 screens' injection-only rule. Violations fail loudly with the
offending file:line.

## Run

```sh
python -m interface.status                # one-shot, real state root
python -m interface.status --demo         # synthetic
python -m interface.watch [--demo]        # Rich Live rotator (keys below)
python -m interface.app.shell [--demo]    # full TUI
```

Every command was executed 2026-09-22 (real root + `--demo`; the TUI is
additionally exercised by the Pilot suite). Watch keys: `p` pauses
auto-rotation (paused still polls — a stalled provider still shows the
STALE banner next to PAUSED), `1..9` jump to a page, space advances,
`q` / Ctrl-C quits. Piped stdin/stdout degrades `watch` to one tier-1
status frame and exit 0.

```sh
# Tests (ephemeral env, nothing installed into the repo):
uv run --no-project --with pytest --with textual==8.2.8 --with rich==15.0.0 \
  --python 3.11 -- python -m pytest interface/tests/ -q
```

## The ONE write path: `:delete <run-id>`

The interface is read-first; its single explicit write is deleting a run
through the core CLI (`collectors/core_cli.py::delete` — the sole
whitelisted WRITE op; every other op still raises `NotImplementedError`).
The path is explicit, confirmed, audited and fail-closed end to end:

1. `:delete <run-id>` (command bar `:` or the ⌃P palette) validates the
   id against the last frame FIRST — an unknown id notifies and never
   opens a dialog.
2. `app/confirm.py` ConfirmDialog: Cancel is the focused default,
   left/right cycle, and Esc OR 30s of silence resolve Cancel — silence
   never action. Both outcomes are audited.
3. On confirm the core delete runs OFF the UI thread behind its own
   SingleFlight slot (never overlapping the spine's polls or another
   delete), then forces one spine refresh (`force_refresh`, queued
   behind any in-flight poll).
4. The activity feed carries the trail, one `[audit]` line each:

   ```
   [audit] ui.delete confirmed run=<id>
   [audit] core.delete ok run=<id> removed=True      # the core's verdict
   ```

   (or `cancelled` / `failed` / `refused` + `error=…` accordingly; a
   failing core surfaces as notify + audit, never a crash). In `--demo`
   or without a state root there is no write lane: confirming refuses
   honestly rather than faking a delete.

**Multi-select is deliberately deferred**: the old registry's batch
delete is not reimplemented as a confirm-loop over single deletes. Batch
deletion returns when a genuine batch write op exists in the core CLI
contract — one audited single delete first (see
`REVIEWS/RETIREMENT.md` §4).

## The M6 absorbed read-only screens (first Option-B wave)

Five old deep-feature screens whose monitoring value outranks their
write risk were absorbed onto the router (`:feeds` `:ledger` `:energy`
`:polymarket` `:sentiment`, all in the palette vocabulary with
descriptions; recents work for free). Each is a copy+adapt extraction:
the DATA contract lives UI-free in `services/`, the screen renders
(read the old file:line citations in each module docstring). The old
tree was not touched.

- **spine-live** — `feeds`, `energy`, `polymarket` poll their service on
  the spine's slow services lane (the ×15 multiplier): on every
  `services`-due they re-fetch behind their own
  `app.spine.SlowLane` (never-overlap single flight + generation-based
  drop-stale delivery), and mirror the spine's services staleness into
  their border badge (▲ stale? / ■ STALE). Table cursors survive every
  repaint.
- **one-shot** — `ledger` (a daily-written file; M6 has no write lane)
  and `sentiment` (a daily report) load on entry / day switch with `r`
  to re-read.
- Fail-closed honesty: `energy` and `polymarket` need an explicit
  pinned-HTTP-proxy config (`STAMMTISCH_ENERGY_PROXY` /
  `STAMMTISCH_POLYMARKET_PROXY`) plus `EIA_API_KEY` for energy —
  ambient proxies are ignored and a missing config renders its error,
  never fabricated rows. `ledger` without a state root says so;
  missing marks are an honest `—`.
- Deliberate deferrals: ledger write ops (log/delete fill) stay in the
  old tree until they ride an audited confirm lane like `:delete`;
  sentiment's `[O]` GALAHAD handoff notifies "M7"; polymarket selection
  no longer launches an external browser (read-only surface); the
  per-symbol sentiment overlay moves with the future domain screens.

The old `tui/` package remains the live deep-feature workstation. Its
M5 retirement was assessed in `REVIEWS/RETIREMENT.md`: **not yet safe**
(6 of 35 old screens matched outright, 20 absent). Committed path:
Option B — keep `tui/` as the deep-feature app, `interface/` as the
monitoring spine; absorb an old screen only when its monitoring value
outranks its write risk. M4 extracted the old tree's in-screen business
logic into `services/` as UI-free copies (the old tree is untouched and
keeps running); nothing here imports it — `tests/test_boundaries.py`
enforces that statically. `interface/` UI copy is English-only per
AGENTS.md rule 7; the old tree's zh chrome is grandfathered until
retirement.
