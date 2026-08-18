# AGENTS.md — STAMMTISCH

> Public repository. Everything committed here must be reproducible by a
> stranger: English only, no personal machine paths, no host-specific
> infrastructure references, no credentials, no internal project narrative.

## What this is

STAMMTISCH is a CLI-first delivery platform that composes independent
review/verification products (doctrine packs such as GALAHAD, the
HIGHBALL rules plane) into pipelines whose
deliverables carry an offline-verifiable evidence bundle.
Delivery reliability outranks every other concern. Read
`docs/architecture.md` before changing anything — it is the normative spec;
this file is the operator's quick reference.

## Layout

- `docs/architecture.md` — normative architecture (state machine, evidence
  store, gates, adapters, roadmap, conformance list);
  `docs/protocol-layer.md` — the wire protocol layer (A2A v1.0 adapter,
  receipt revisions, binding discipline).
- `schemas/` — the seven STAMMTISCH contracts (pipeline, run manifest,
  bundle manifest, gate record, run event, a2a invocation receipt,
  cost ledger).
- `src/` — Rust core. Key modules: `store.rs` (atomic writes, fsynced event
  log, launch lock), `runner.rs` (state machine + projection fold),
  `gates.rs` (deterministic gate evaluator), `adapters/` (`fake.rs` canned
  products for offline tests, `galahad.rs` / `highball.rs` shipped
  product CLIs, `a2a/` the wire runtime for QUINTE),
  `bundle.rs` (export/offline verify), `jsonval.rs` (embedded JSON Schema
  validator — no network schema fetches).
- `pipelines/examples/`, `doctrine/examples/` — the offline example slice.
- `tests/conformance.rs` — the §11 conformance suite.

## Build and test

```bash
cargo build --release        # must stay warning-free
cargo test                   # all green before any push
STAMMTISCH_HOME=$(mktemp -d) target/release/stammtisch-core validate \
  --pipeline pipelines/examples/security.json --json
# The shipped security.json targets a real A2A endpoint (QUINTE
# review stage): replace the example.invalid endpoint + token_env and
# export A2A_TOKEN before `run`. Offline runs use the fake-adapter
# pipelines built by tests/conformance.rs.
```

## Non-negotiable rules for contributors (human or agent)

1. **Events are the authority.** Never trust `manifest.json` on read paths;
   fold from `events.jsonl`. Never edit events, receipts, manifests, gate
   records, or bundle files to change state.
2. **Fail closed.** Corrupt state, unknown contract revision, digest drift,
   missing metric → halt with a durable record. No best-effort parsing, no
   silent fallback to fake adapters (a resolved-but-broken real config is a
   hard error).
3. **Gates are code.** Acceptance criteria are quantified thresholds over
   typed artifact fields, never model judgment.
4. **Adapters own all product contact.** Core modules never spawn product
   processes directly. Real product invocations go through `adapters/` and
   record receipts content-addressed.
5. **Tests are part of the deliverable.** New behavior ships with tests;
   the tamper-detection conformance tests must keep passing.
6. **Offline first.** Shipped examples and the default test suite run fully
   offline (`"adapter": "fake"`). Real-product integration tests stay
   env-gated (`STAMMTISCH_IT=1`) and never run in CI by default.
7. **Public-repo hygiene.** Framework files and UI copy are English; fixtures
   and source-language processing data may preserve the text they exercise.
   No host paths, ports, proxy details, credentials, or internal-only project
   references.
8. **Preserve contributor identity.** A commit authored by an agent must use
   that agent's GitHub-linked Git author identity; do not replace it with the
   human operator or rely only on a co-author trailer. Human-authored commits
   keep the human author and may acknowledge assistance with trailers.
