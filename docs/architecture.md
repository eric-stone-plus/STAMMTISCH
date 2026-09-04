# STAMMTISCH Architecture

Status: v0 draft (2026-08-09) · Normative language: RFC 2119 style ("must/should/may").

STAMMTISCH is a **CLI-first delivery platform** that composes independent
review/verification products into end-to-end, evidence-backed pipelines.
Its product promise is one sentence:

> **Every deliverable ships with a bundle that a skeptical third party can verify offline.**

Everything in this document is subordinate to that promise. When a design
choice trades reliability against convenience, reliability wins.

---

## 1. Positioning

STAMMTISCH is an orchestrator of *products*, not an agent runtime. Its
deterministic core does not call model endpoints, own prompts, or reimplement
review logic. An optional local workstation may offer model-backed chat and
tools, but those interactions never decide pipeline state or gate outcomes.
The composition has two tiers — two protagonists and one rules plane:

**Protagonists** (the intelligence the pipeline exists to harness):

| Component | Role | Contract surface |
|---|---|---|
| Doctrine pack (first instance: **GALAHAD**) | Domain briefs, research doctrine, quantified acceptance gates | Directory of declarative documents |
| **QUINTE** | Review of the brief over the A2A wire protocol (Result 2.1 shape) | versioned A2A invocation receipts + `review.result` artifact |

**Rules plane** (deterministic support contracts, no intelligence of its own):

| Component | Role | Contract surface |
|---|---|---|
| **HIGHBALL** | Delivery-side rules: claim routing (direct-evidence / human-review / block), authorization gate, protected-write guard | routing contracts, Action Packet schema, validators |

Explicitly **not** part of the composition: host infrastructure such as
network egress gateways, systemd, containers, and storage. Pipelines consume
infrastructure; they are not orchestrated by STAMMTISCH and carry no
pipeline semantics. (A network egress gateway, however supervised, is
plumbing: different domain, different blast radius, different narrative
discipline from the delivery rules plane — the two rule-flavored systems
share an abstraction level but must not share a repository.)

STAMMTISCH itself contributes: **pipeline state machine, evidence store,
gate evaluation, run registry, and the deliverable bundle format.**

### Non-goals (v0)

- No hosted pipeline service or core daemon. The authoritative surface is a
  CLI and state root; an optional local TUI is a consumer of that surface.
- No agent framework (no prompt chains, no tool-calling loops of its own).
- No live capital / production deployment actions. Pipelines produce
  *decision-grade deliverables*; execution stays outside.
- No reimplementation of product internals. If a product lacks a
  contract, STAMMTISCH wraps it at the CLI level or does not integrate it.

---

## 2. Design principles (delivery reliability first)

P1. **Deterministic core.** The pipeline runner is a state machine in code.
Model output never decides control flow; it only produces *artifacts* that
the state machine evaluates.

P2. **Content-addressed everything.** Every input, artifact, receipt, and
result enters the evidence store by SHA-256 digest. A run manifest chains
them. Tampering after the fact is detectable by construction.

P3. **Fail-closed.** Ambiguity — corrupt state, unknown run, digest drift,
unparseable receipt — halts the pipeline with a durable `HALTED` record.
The runner never guesses, never silently skips, never auto-retries a
protected step.

P4. **Gates are quantified and evaluated in code.** "The model feels done"
is never an acceptance criterion. Gates come from the doctrine pack and are
evaluated deterministically against artifact content.

P5. **Recovery is explicit.** After a crash, `stammtisch reconcile` binds
durable state and reports; it never advances work. Resumption is a separate,
audited action.

P6. **Human authority is routed, not assumed.** Protected actions (external
writes, deletions, anything irreversible) go through the control plane's
authorization gate with an explicit, recorded decision.

P7. **Thin platform, thick products.** STAMMTISCH code stays small; the
intelligence lives in the products. If a pipeline stage can be delegated to
a product, it is.

---

## 3. Domain model

```
DoctrinePack   versioned directory: briefs, gate definitions, domain schemas
Pipeline       declarative spec (JSON, canonical digest) — ordered Stages
Stage          one invocation of a Product with declared inputs/outputs/gate
Run            one execution of a Pipeline — state machine instance + evidence
Artifact       any file a stage produces; stored by digest
Receipt        a product-emitted record (HIGHBALL packet, A2A invocation
               receipt, …) validated against its schema before acceptance
Gate           quantified predicate over stage artifacts — pass/fail/halt
EvidenceBundle the deliverable: manifest + artifacts + receipts + gate log
Deliverable    an EvidenceBundle whose final gate passed
```

A **Pipeline** is the unit of work design. A **Run** is the unit of
execution. An **EvidenceBundle** is the unit of delivery.

### 3.1 Pipeline spec (v0 shape)

```jsonc
{
  "schema": "stammtisch.pipeline.v0",
  "id": "security-daily",
  "doctrine": {"pack": "galahad", "ref": "digest or path"},
  "stages": [
    {"id": "brief",    "product": "doctrine", "out": ["brief.json"]},
    {"id": "review",   "product": "quinte",   "in": ["brief.json"],
     "out": ["review.result", "highball.route-request.json",
             "highball.residual-trace.json"], "gate": "quinte_result_21",
     "on_block": "blocked"},
    {"id": "deliver",  "product": "highball",
     "in": ["review.result", "highball.route-request.json",
            "highball.residual-trace.json"],
     "gate": "packet_authorized"}
  ]
}
```

Canonicalization: the pipeline spec is parsed, normalized, and serialized
before binding; the canonical digest is the run's provenance root (same
discipline as brief digests).

The review task returns the review result and both HIGHBALL carriers as typed
outputs of the same A2A task. The carriers preserve the original route request
and residual closure evidence; STAMMTISCH does not synthesize either from a
review summary. The HIGHBALL adapter resolves only the two declared carrier
digests from the run artifact store, then lets HIGHBALL validate their action
binding and closure semantics. A product work directory is never an evidence
source for this boundary.

---

## 4. Run state machine

```
created → staged → running → gating → completed
                    │          ├──────→ halted  (fail-closed stop)
                    │          └──────→ blocked (gate refused, evidence kept)
                    ├─────────────────→ failed  (product failure, terminal)
                    └─────────────────→ cancelled (explicit operator action)
```

Rules:

- Terminal states: `completed`, `blocked`, `failed`, `halted`, `cancelled`.
- `completed` requires *every* stage receipt schema-valid, every gate passed,
  and the final bundle digest recorded. Anything less is not completed.
- `blocked` is a **success-shaped** terminal state: the pipeline worked, the
  gate refused, the evidence says why. Blocked deliverables never ship.
- Transitions are recorded in `events.jsonl` (append-only, fsynced) before
  any in-memory state is trusted. The manifest is a projection; events are
  the authority.
- One active run per state root by default (serialized through a launch
  lock); parallelism is per-run internal.

---

## 5. Adapters

Adapters are the only code allowed to touch a product. Each adapter
implements:

```
preflight()   -> capability/availability probe (advisory)
invoke(spec)  -> launch one product invocation, return InvocationHandle
poll(handle)  -> one-shot status read (a wire adapter may loop with bounded
                 sleeps, budgeted by the stage's timeout_seconds)
collect(handle) -> { receipts[], artifacts[], verdict } — schema-validated
```

A stage with a `runtime` block resolves to the wire adapter
(`src/adapters/a2a/`, docs/protocol-layer.md). A highball or galahad stage
without `adapter: "fake"` resolves to the shipped product CLI
(`src/adapters/highball.rs`, `src/adapters/galahad.rs`). Everything else
resolves through its offline fake. No ambient environment selection.

### 5.1 HIGHBALL adapter

Routes claims (`route-residual-action`), validates Action Packets before any
protected step, and records the authorization decision into the evidence
bundle. STAMMTISCH never performs a protected action without a HIGHBALL
packet; the absence of a packet is itself fail-closed.

### 5.2 Doctrine adapter

Loads the doctrine pack (gate definitions, brief templates, domain schemas),
verifies pack digest, and materializes the stage-0 brief. Doctrine changes
between runs are visible as digest drift in provenance. The offline
`adapter: "fake"` path stays this template renderer.

### 5.2b GALAHAD adapter

Invokes the shipped GALAHAD paper product (`galahad-futures`
`run_paper.py --source fixture`). The collected artifact carries a
selection identity (`run_id` / `as_of` / symbol / strategy) plus target
positions, or an explicit `NO-GO` / no-trade. Live mode is refused.
STAMMTISCH does not spawn QUINTE.

A stage declaring `engine: "nautilus_live"` is a controlled Binance
**testnet** pass-through — never mainnet. Acceptance requires ALL THREE,
fail-closed otherwise: (1) the schema-validated stage engine
`"nautilus_live"`, (2) a product summary reporting `mode: "testnet"`
(the product never reports `"live"` for these sessions), and
(3) `GALAHAD_ENABLE_TESTNET=1` in the adapter's environment at run time;
a declared-but-ungated stage fails before any product contact
(`galahad_testnet_disabled`, usage error), and a testnet summary without
the declared engine is `galahad_testnet_undeclared`. Any other mode
string — mainnet `"live"` included — stays refused
(`galahad_live_refused`). Accepted testnet sessions receipt as
`galahad.testnet-session.v1` (venue + reconciliation digest fields:
`orders_submitted` / `orders_filled` / `position_mismatch`); paper
sessions keep `galahad.paper-session.v1`. No shipped example pipeline
uses it: the offline-first rule is unchanged.

### 5.3 A2A adapter (wire runtime)

Products with a `runtime` binding (QUINTE and any future wire-backed
product) run through the A2A v1.0 adapter (docs/protocol-layer.md): Agent
Card discovery at preflight, `SendMessage` at invoke, a bounded `GetTask`
loop at poll, terminal snapshot + artifact mapping at collect. Every wire
observation is wrapped into an `a2a.invocation.v2` receipt pinned to the
observed card digest, endpoint, and verbatim upstream payload; a timed-out
or input-requiring task halts without being cancelled (operator handoff
with the task id and endpoint in the halt detail).

---

## 6. Evidence store and deliverable bundle

State root (default `~/.local/share/stammtisch`, overridable, always pinned
per run):

```
<state-root>/
  pipelines/<id>.json                 # canonical specs
  runs/<run-id>/                      # UUIDv7
    manifest.json                     # projection, rebuilt from events
    events.jsonl                      # authority: transitions, digests
    receipts/<stage>.<n>.json         # validated product receipts
    artifacts/<sha256>                # content-addressed store
    gates/<stage>.gate.json           # gate evaluation record
    cost.json                         # per-run cost ledger (see below)
    bundle/                           # assembled on completion
      MANIFEST.json                   # ordered digest chain
      ... exported artifacts + receipts + gate log
  host/launch.lock                    # one-active serialization
```

Bundle rules:

- `MANIFEST.json` lists every artifact/receipt/gate record with SHA-256,
  in pipeline order, plus the pipeline canonical digest and doctrine digest.
- `stammtisch verify --bundle DIR` re-checks all digests, re-validates all
  receipts against their schemas, and re-evaluates gates from artifacts —
  offline, without any product installed. Verification is a pure function of
  bundle bytes.
- Export is `stammtisch export RUN_ID --out DIR` (or tar). The bundle is the
  only supported handoff format.

### 6.1 Per-run cost ledger

Every run produces `cost.json` (`stammtisch.cost-ledger.v0`): one entry per
stage with invocation count, observation (receipt) count, wall-clock seconds
of product contact (preflight through collect), and token usage when the
receipts carry it.

- **Additive and fail-safe.** The ledger never invents numbers: receipts
  without usage fields record `null` tokens. A ledger write failure is
  swallowed — cost accounting never blocks or alters a run outcome, and a
  run without `cost.json` still exports and verifies (the ledger is simply
  absent from the manifest).
- **Usage extraction.** Receipt contracts do not standardize token fields.
  The ledger reads `upstream.usage` (or `upstream.task.metadata.usage`)
  for `input_tokens` / `output_tokens` / `total_tokens` when present;
  anything else records `null`.
- **Invocation semantics.** A2A receipts are wire observations, not
  invocations: one task (`task_id`) counts as one invocation regardless of
  how many observations it produced; CLI product stages count one invocation
  per receipt.
- **Verification.** `verify` re-validates a bundled `cost.json` against its
  contract like every other bundled artifact.

---

## 7. Gates

A gate definition (from the doctrine pack) binds:

```jsonc
{
  "id": "packet_authorized",
  "kind": "artifact_flag",             // | schema_check | receipt_flag | metric_threshold
  "artifact": "deliver.packet.json",
  "flag": "action_decision",
  "op": "==", "value": "pass",
  "on_fail": "halted"                   // blocked | halted
}
```

- Gate evaluation is deterministic code over parsed artifacts — never an
  LLM judgment call. (A product may use LLMs *internally* to produce the
  artifact; the gate reads only the artifact's typed fields.)
  Live QUINTE review uses `quinte_result` (Result 2.1 shape:
  `result_version`, `status`, `run_id`, `residuals`, `recommendation`)
  rather than `walkforward_min_sharpe`. HIGHBALL product stages
  read `action_decision` via `artifact_flag`.
- Every gate evaluation emits `gates/<stage>.gate.json` with the observed
  value, threshold, decision, and the artifact digest it read.
- Unknown gate kind, missing metric, or unparsable artifact ⇒ fail-closed.

---

## 8. CLI surface (v0)

```text
stammtisch init                         # create state root
stammtisch validate --pipeline FILE     # spec + schema check
stammtisch run --pipeline FILE          # launch (acquires launch lock)
stammtisch status [RUN_ID]              # one-shot projection
stammtisch inspect RUN_ID               # receipts, gates, verdicts
stammtisch reconcile                    # bind durable state after crash
stammtisch resume RUN_ID                # explicit, audited
stammtisch cancel RUN_ID                # explicit operator action
stammtisch cancel --abandoned           # seal leftover non-terminal runs
                                        # whose core is gone (TUI start/quit)
stammtisch export RUN_ID --out DIR      # assemble deliverable bundle
stammtisch verify --bundle DIR          # offline third-party verification
        [--signature FILE] [--public-key FILE]
                                        # optional minisign check over MANIFEST.json
stammtisch doctrine show|digest         # inspect doctrine pack binding
```

Exit codes: `0` completed-or-clean observation, `1` product-failure family,
`2` blocked/halted (gate or integrity), `3` usage/contract error. Machine
output is always `--json` envelope; stderr stays human text.

---

## 9. Technology choices

- **Core: Rust** (single static binary `stammtisch`). Rationale: the state
  machine, evidence store, and gate evaluator are the reliability-critical
  core and benefit from exhaustive types, explicit error taxonomy, and
  deterministic serialization. No async runtime in v0 — polling is
  one-shot by design; the CLI never holds long-lived loops.
- **Contracts: JSON Schema**, one file per contract under `schemas/`,
  versioned independently of the package version (`stammtisch.pipeline.v0`,
  immutable `stammtisch.manifest.v0` reader plus the current
  `stammtisch.manifest.v1` projection, `stammtisch.bundle.v0`, gate record,
  and run event). Manifest v1 adds the event-pinned bundle digest; historical
  v0 runs remain inspectable but cannot be newly exported without that proof.
- **Products: any language**, integrated only through the adapters in §5.
- **Minimal network stack** in the core: only versioned product adapters may
  contact declared endpoints. The pipeline runner and gates never infer
  behavior from ambient network state; credentials stay in named env vars.

---

## 10. Phased roadmap (exit criteria fixed in advance)

**P0 — skeleton + simulated slice (offline).**
Core state machine, evidence store, gate evaluator, `validate/run/status/
inspect/export/verify`, fake-product adapters (contract-accurate canned
receipts). Exit: 100% unit tests green incl. the conformance list in §11;
`verify` passes on a bundle produced by a simulated quant slice; tamper
tests (flip one artifact byte, one receipt field) all detected.

**P1 — real product stages.**
A real product runtime behind an adapter, one-active discipline, a real
security pipeline end to end with doctrine gates, still offline-
verifiable bundles. Exit: 5 consecutive real runs with valid bundles;
crash-during-run recovery demonstrated via `reconcile` without duplicate
stage invocation; digest drift on doctrine change detected.

**P1.5 — dual-engine GALAHAD stage.**
The galahad-futures product selects its execution backend per stage via
the schema-validated `engine` field ("paper" reference book, default; or
"nautilus", the pinned NautilusTrader 1.231.0 event-driven backtest
engine). The adapter appends `--engine` to the product CLI, refuses any
other value, and fail-closes when the product summary's `engine` field
disagrees with the declared stage parameter (legacy summaries without
the field read as "paper"). The product's dual-engine parity report
(`run_parity.py`, schema `galahad.parity.v1`) is a research artifact
consumed by the review stages, not a promotion gate: the reference book
remains the arbiter. Exit: a stage declaring `engine: "nautilus"`
completes with a valid bundle whose summary carries the matching engine
identity; a mismatched or absent backend fails closed with
`galahad_engine_mismatch` / a product error, never a silent fallback.

**P2 — HIGHBALL stages.**
Claim routing per class; authorization packets in the bundle.
Exit: a HIGHBALL DENIED decision terminates the run per the stage's
`on_block` policy with the refusal recorded in the evidence (the §11.8
conformance test already drives this path through the fake adapter); a
protected action without packet halts the run.

**P3 — hardening.**
Cost/token accounting per run, fsync-parent-dir durability, bundle
signature (optional minisign), conformance suite published.
Exit criteria written before P3 starts.

Status as of 2026-08-18:

- **Landed: cost ledger** (`cost.json` per run, §6.1), bundled with its own
  manifest entry (kind `cost`) and re-validated by `verify`; runner stage
  wall-time accounting (product contact phase); unit + conformance tests.
- **Landed: bundle signature hook.** `verify --bundle DIR --signature FILE
  [--public-key FILE]` runs `minisign -V -m MANIFEST.json -x FILE [-p KEY]`
  when `minisign` is on PATH. Fail-closed: a requested signature with
  minisign absent, a missing signature file, or a rejected signature is an
  error — never a silent skip. Export never calls minisign; the hook is
  purely an offline verification step. Tests stub minisign and the error
  paths, so the suite runs without minisign installed.
- **Landed: fsync-parent-dir durability.** `atomic_write` fsyncs the
  renamed file and then fsyncs the parent directory (POSIX `O_DIRECTORY`
  open + `fsync`) so a crash immediately after rename cannot drop the
  directory entry. Covered by the unit test on the real helper.
- **Landed: GALAHAD testnet pass-through.** `engine: "nautilus_live"`
  runs a Binance testnet session behind the three-condition gate in
  §5.2b (declared engine + summary `mode: "testnet"` +
  `GALAHAD_ENABLE_TESTNET=1`); accepted sessions receipt as
  `galahad.testnet-session.v1`, mainnet stays refused everywhere.
- **Remaining: conformance suite publication.** The conformance suite lives
  in-tree (`tests/conformance.rs`, §11 items 1–10 plus the P3 additions) and
  runs under `cargo test`; it is not published as a standalone artifact.

---

## 11. Conformance and reliability test strategy

The test suite is part of the deliverable, not an afterthought:

1. Schema-valid receipts accepted; schema-invalid rejected (every contract).
2. One-active: concurrent `run` calls serialize; at most one active run.
3. Corrupt run dir / unknown run ⇒ fail-closed with durable HALTED record.
4. Crash after stage launch, before receipt ⇒ reconcile binds, no relaunch.
5. Artifact tamper, receipt tamper, gate-log tamper ⇒ `verify` fails closed.
6. Gate threshold boundary values (== vs >, missing metric, NaN) exact.
7. Events-first durability: manifest deleted ⇒ fully rebuilt from events.
8. Blocked pipeline ships nothing: `export` refuses non-completed runs.
9. Deterministic replay: same bundle bytes ⇒ same verification verdict,
   byte-identical report.
10. Adapter contract drift: product emitting an unknown contract revision ⇒
    halt, never parse-best-effort.
11. Cost ledger: a completed run ships `cost.json` (kind `cost`, digest
    matched, contract-valid) and `verify` detects a tampered ledger offline.

---

## 12. Risks and open questions

- **Product contract drift** (products iterate on their own schedules).
  Mitigation: adapters pin contract revisions; unknown revision halts;
  conformance suite runs in CI against product fixtures.
- **Bundle size** for artifact-heavy pipelines. Mitigation: content-
  addressed store with export-time inclusion list; large raw data stays out
  of the bundle by default (digest reference only).
- **Gate expressiveness vs. safety.** A gate DSL is deliberately tiny in v0
  (metric thresholds, schema checks, receipt flags). Richer logic must be
  justified by the quant slice before admission.
- **Multi-run concurrency** is deferred; one-active is the v0 contract.
- **Rules-plane absorption.** HIGHBALL is consumed as a standalone product
  today. If routing/authorization rules ever get maintained in two places
  (here and there), evaluate folding HIGHBALL into STAMMTISCH as a
  `rules/` contract library. Not now: it has an independent public identity
  and live contract alignment with its host platform. What must never happen
  is merging delivery rules with network egress plumbing — different
  domains.

---

## Appendix A. Why not X

- **Not a monorepo merge of the composed products.** They evolve independently;
  composition through contracts keeps each replaceable (worker products are
  commodities; the evidence layer is the durable asset).
- **Not Python glue.** The reliability claims in §6–§10 want a typed,
  deterministic core with a single-artifact CLI distribution.
- **Not a service.** A state root + CLI is inspectable, backable-up, and
  matches how the products already run on this host.
