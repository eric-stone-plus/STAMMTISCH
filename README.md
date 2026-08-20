# STAMMTISCH

**CLI-first agentic delivery platform — deterministic pipelines, adversarial verification, audit-grade evidence.**

STAMMTISCH is the quant workstation: the surface users open to read data,
run workbenches, and drive pipelines. Its analysis identity is
[GALAHAD](https://github.com/eric-stone-plus/GALAHAD) — the assistant
users talk to. The adversarial review orchestrator (QUINTE) and the
delivery rules plane (HIGHBALL) are internal pipeline mechanisms:
developer docs name them, the user surface does not.

## Quick start

```sh
# One-click base workstation (STAMMTISCH core + examples only).
# The review orchestrator, rules plane, analysis identity, philosophy,
# and optional egress gateway are separate opt-in installs —
# see docs/fullstack-quickstart.md.
curl -fsSL https://raw.githubusercontent.com/eric-stone-plus/STAMMTISCH/main/scripts/bootstrap-fullstack.sh | sh

# One command does everything:
stammtisch               # → TUI workstation
stammtisch init          # → initialize state root
stammtisch run --pipeline pipelines/examples/security.json
stammtisch status        # → list all runs
stammtisch inspect RUN   # → receipts, gates, verdicts
stammtisch export RUN --out /tmp/bundle
stammtisch verify --bundle /tmp/bundle
stammtisch config        # → show TUI config (API key masked)
stammtisch config set-key sk-...   # → save the AI key (prompts if omitted)
```

No args = TUI. With args = CLI.

## Setup

```sh
# Build the Rust core
cargo build --release

# Install TUI dependencies
uv venv --clear --python 3.11 .venv
uv pip sync --python .venv/bin/python --require-hashes requirements-tui.lock

# Symlink to PATH (optional)
ln -sf "$(pwd)/stammtisch" ~/.local/bin/stammtisch
```

Optional integrations (the TUI degrades gracefully without them):

```sh
export STAMMTISCH_PYTHON=/path/to/python   # interpreter used for the TUI
export GLM_API_KEY=...                     # chat + run analysis (GLM default;
                                           #  DEEPSEEK_API_KEY still honored)
# or persist the key in the config file instead: stammtisch config set-key
# quantkit importable in the TUI python → quant engine commands
```

`requirements-tui.txt` declares only the two direct UI dependencies.
`requirements-tui.lock` fixes their complete transitive environment with
distribution hashes for Python 3.11 on x86-64 Linux. It deliberately excludes
quantkit and the wider quantitative environment. Regenerate it explicitly with
the command recorded in its header; install optional integrations after the
minimal UI environment is rebuilt, or point `STAMMTISCH_PYTHON` at a separately
managed compatible environment.

## TUI

Dashboard keys: `A` Ask, `E` edit config, `C` crawlers panel, `L`
language (English/简体中文), click selects one run,
`Shift+click` selects a range, `Ctrl+A` selects all, `Delete` removes the
selection, `Shift+D` delete all listed pipeline runs, `Q` quit; `Enter`
inspects the cursor row. The registry shows session name and time in
separate columns.
The Plugins sidebar (alphabetical) lists pipeline workbenches plus the
CRYPTO (Polymarket tape) and ENERGY (EIA watchlist) modules. The quant and
daily-report functions live inside the SECURITY (equity board by market
zone) and FUTURES workbenches, not on the dashboard.
The Quick Start sidebar is mouse-clickable.
See [`tui/README.md`](tui/README.md).

## Architecture

See [`docs/architecture.md`](docs/architecture.md) (normative spec),
[`docs/protocol-layer.md`](docs/protocol-layer.md) (wire protocol), and
[`docs/rashomon.md`](docs/rashomon.md) (how the five RASHOMON gates are
interpreted and enforced across the stack).

New to the whole stack? Start at
[`docs/fullstack-quickstart.md`](docs/fullstack-quickstart.md) — the
single entry point that reproduces the complete workstation from the
six public repositories, including the reproducibility boundary (what
is public mechanism vs. your private content).

```
stammtisch              ← unified launcher (TUI / CLI)
├── src/                ← Rust core (stammtisch-core binary)
│   ├── runner.rs       ← run state machine
│   ├── store.rs        ← evidence store (events.jsonl authority, fsynced)
│   ├── gates.rs        ← deterministic gate evaluator
│   ├── bundle.rs       ← export / offline verify
│   ├── jsonval.rs      ← embedded JSON Schema validator (no network)
│   ├── doctrine.rs     ← doctrine pack loader (digest-pinned)
│   ├── adapters/       ← the only code allowed to touch products
│   │   ├── fake.rs     ← offline fakes with contract-accurate receipts
│   │   ├── galahad.rs  ← shipped GALAHAD paper product CLI
│   │   ├── highball.rs ← HIGHBALL rules plane (Action Packets)
│   │   └── a2a/        ← A2A v1.0 wire runtime (QUINTE review)
│   └── cmd.rs          ← CLI surface
├── tui/                ← Python TUI (textual + rich, nmtui style)
│   ├── app.py          ← app + global bindings
│   ├── screens/        ← screen package: dashboard, domains, intake, runs, chat, config
│   ├── analysis.py     ← quant screens (data/backtest/indicators/portfolio/gates)
│   ├── intake.py       ← verified daily-data product adapter
│   ├── widgets.py      ← stage flow, gate cards, HUD
│   ├── driver.py       ← stammtisch-core CLI driver
│   ├── engine.py       ← quantkit bridge (optional)
│   ├── ai_driver.py    ← AI chat driver, OpenAI-compatible (optional)
│   ├── config.py       ← file config + env overrides (0600)
│   └── theme.py        ← grayscale CSS
├── schemas/            ← JSON Schema contracts (versioned)
├── pipelines/examples/ ← example pipeline specs
└── doctrine/examples/  ← example doctrine packs
```

## Technical stack

### Host mechanics

STAMMTISCH is an **orchestrator of products, not an agent runtime**. The
deterministic core never calls model endpoints, owns prompts, or reimplements
review logic. Composition has two tiers:

- **Protagonists** — the intelligence the pipeline exists to harness:
  doctrine packs (domain briefs, research doctrine, quantified acceptance
  gates; first instance **GALAHAD**) and **QUINTE** (adversarial review of
  the brief over the A2A wire protocol).
- **Rules plane** — deterministic support contracts, no intelligence of
  their own: **HIGHBALL** (claim routing, authorization gates, protected-
  write guards via Action Packets).

**Run state machine** (`created → staged → running → gating → completed`,
with fail-closed branches to `halted` / `blocked` / `failed` / `cancelled`):

- Terminal states are explicit; `blocked` is success-shaped (the pipeline
  worked, the gate refused, the evidence says why — deliverables never ship).
- **Events are the authority**: every transition is appended to
  `runs/<id>/events.jsonl` (fsynced) before any in-memory state is trusted;
  `manifest.json` is a projection rebuilt from events.
- One active run per state root (launch lock); parallelism is per-run.
- Crash recovery via `reconcile`: no duplicate stage invocation.

**Evidence store** (state root, pinned per run): receipts
(`receipts/<stage>.<n>.json`, schema-validated), content-addressed artifacts
(`artifacts/<sha256>`), gate records (`gates/<stage>.gate.json`), and the
assembled bundle. `stammtisch verify --bundle DIR` re-checks every digest,
re-validates every receipt, and re-evaluates every gate **offline** — a pure
function of bundle bytes, with no product installed. `--signature FILE` with
`--public-key FILE` adds a minisign detached-signature check over the bundle
manifest, fail-closed when the signature or `minisign` is absent.

**Gates** are quantified thresholds over typed artifact fields —
`artifact_flag`, `schema_check`, `receipt_flag`, `metric_threshold`,
`quinte_result` — never LLM judgment. Unknown gate kind, missing metric, or
unparsable artifact fail closed.

### Engines (product adapters)

Adapters are the only code allowed to touch a product. Every adapter
implements the same contract:

```
preflight() → capability/availability probe (advisory)
invoke()    → launch one product invocation → InvocationHandle
poll()      → one-shot status read (bounded by stage timeout_seconds)
collect()   → { receipts[], artifacts[], verdict }, schema-validated
drain()     → receipts observed outside a successful collect (failure paths
              keep their evidence)
```

Resolution: a stage with a `runtime` block → A2A wire adapter (QUINTE); a
`highball`/`galahad` stage without `adapter: "fake"` → the shipped product
CLI; everything else → its offline fake. No ambient environment selection.

**GALAHAD engine selection** — the shipped paper product
(`galahad-futures/run_paper.py`) runs one of two execution backends behind
the same decision layer:

```jsonc
{ "id": "paper", "product": "galahad", "engine": "nautilus",
  "workdir": "/path/to/galahad-futures", "out": ["galahad.summary.json"] }
```

- `"paper"` (default) — the reference accounting book: close-price fills,
  per-bar funding, margin caps, liquidation. The arbiter.
- `"nautilus"` — NautilusTrader 1.231.0 event-driven backtest engine
  (pinned): synthetic L1 books from the OHLC bars, taker fees, venue
  margin machinery. Optional dependency in the product tree.

The adapter appends `--engine`, refuses any other value, and fail-closes
when the product summary's `engine` field disagrees with the declared stage
parameter — a missing backend is a product error, never a silent fallback.
The product's dual-engine parity report (`galahad.parity.v1`) reconciles
decision streams, equity curves, fills, funding, boundary crossings, and
threshold sensitivity; it is research evidence for the review stages, not a
promotion gate.

**HIGHBALL adapter** routes claims, validates Action Packets before any
protected step, and records the authorization decision into the bundle —
STAMMTISCH never performs a protected action without a packet, and a missing
packet is itself fail-closed.

### Protocols

Wire-backed products speak **A2A v1.0** (JSON-RPC 2.0 over HTTP), the Linux
Foundation agent-interop protocol:

```
runner / gates / evidence store        (wire-agnostic)
        │  Adapter trait (the pluggable-product seam)
        ▼
a2a adapter:  Agent Card discovery (preflight)
              SendMessage           (invoke)
              bounded GetTask loop  (poll)
              terminal snapshot → artifact + receipt  (collect)
        │
        ▼
A2A v1.0 over HTTP — any conforming agent
(ADK, LangGraph, CrewAI, AutoGen, a2a-rs, proprietary hosts)
```

- Every wire observation is wrapped into an `a2a.invocation.v2` receipt
  pinned to the observed Agent Card digest, endpoint, and verbatim upstream
  payload.
- A timed-out or input-requiring task **halts without being cancelled** —
  operator handoff carries the task id and endpoint in the halt detail.
- A different wire protocol later means a new adapter module and a new
  `runtime.protocol` value — never a runner, gate, or evidence change.

**Contract schemas** (versioned JSON Schema, one per file under `schemas/`):
pipeline (`stammtisch.pipeline.v0`), run manifest (immutable `.v0` reader +
`.v1` projection with event-pinned bundle digest), bundle manifest, gate
record, run event, `a2a.invocation.v1` / `.v2` receipts, and the per-run cost
ledger (`stammtisch.cost-ledger.v0`).

### Technology choices

- **Core: Rust**, single static binary. The state machine, evidence store,
  and gate evaluator are the reliability-critical core: exhaustive types,
  explicit error taxonomy, deterministic serialization. No async runtime in
  v0 — polling is one-shot by design.
- **Products: any language**, integrated only through adapters.
- **Minimal network stack**: only versioned product adapters contact
  declared endpoints; credentials stay in named env vars
  (`A2A_TOKEN`, `GALAHAD_PYTHON`, ...).
- **Embedded JSON Schema validator** (`jsonval.rs`) — no network schema
  fetches.

## License

Apache-2.0 (see [`LICENSE`](LICENSE)).
