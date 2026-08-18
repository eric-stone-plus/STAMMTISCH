# The protocol layer

How STAMMTISCH talks to real, wire-backed products — today the QUINTE
review product, tomorrow any agent that speaks the same protocol. This
document is the design record for `src/adapters/a2a/`, the
versioned A2A invocation receipt revisions, and the `runtime` stage binding in
the pipeline contract. `docs/architecture.md` stays the normative spec;
this file is the rationale and the mapping.

## 1. Why a protocol layer

QUINTE's first adapter was a client of a bespoke, CLI-based host contract
(`host preflight/start/status/inspect/reconcile` with a JSON stdout
envelope). That contract was removed together with the product. Two facts
changed the requirements for its replacement:

1. **The host is no longer fixed.** The base model behind QUINTE is now
   decided (one vendor family, DeepSeek — QUINTE's single-vendor doctrine),
   but that binding is policy, not wire: it may change again, and the
   protocol layer must not encode a vendor.
2. **The industry converged on a vendor-neutral wire standard for exactly
   this shape.** The Agent2Agent protocol (A2A), donated to the Linux
   Foundation with 150+ supporting organizations and production
   deployments across supply chain, financial services, insurance, and IT
   operations, standardizes agent discovery (Agent Cards), a task
   lifecycle with pollable state, and artifact-based results — which is
   structurally identical to STAMMTISCH's `invoke/poll/collect` contract.

So the protocol layer is: **A2A v1.0 (JSON-RPC over HTTP) as the wire
protocol, the existing `Adapter` trait as the abstraction seam, and
STAMMTISCH-native receipt revisions as the evidence contract.** Any A2A
v1.0 agent — built with any framework, backed by any model — can serve as
a product; the runner never learns which one.

## 2. The layers

```
┌────────────────────────────────────────────────────────────┐
│  runner / gates / evidence store          (unchanged)      │
│  pipeline spec: stages declare `runtime`  (new field)      │
├────────────────────────────────────────────────────────────┤
│  Adapter trait: preflight/invoke/poll/collect/drain        │
│            (unchanged; the seam that makes products        │
│             pluggable)                                     │
├────────────────────────────────────────────────────────────┤
│  A2A v1.0 adapter (src/adapters/a2a/)      (new)           │
│    model.rs  typed wire model (spec-shaped)                │
│    client.rs JSON-RPC + Agent Card discovery               │
│    mod.rs    lifecycle mapping + receipts + fail-closed    │
├────────────────────────────────────────────────────────────┤
│  A2A v1.0 over HTTP (JSON-RPC 2.0)          (external)     │
│  any conforming agent: ADK, LangGraph, CrewAI, AutoGen,    │
│  a2a-rs, proprietary hosts — behind any base model         │
└────────────────────────────────────────────────────────────┘
```

Everything above the seam stays wire-agnostic. A different wire protocol
later means a new adapter module and a new `runtime.protocol` value —
never a runner, gate, or evidence change.

## 3. Lifecycle mapping

| STAMMTISCH phase | A2A operation | Notes |
|---|---|---|
| `preflight` | GET `/.well-known/agent-card.json` | Interface negotiation: the card must offer `protocolBinding: "JSONRPC"` at `protocolVersion: "1.0"`. The card's canonical digest is pinned and recorded; a stage-configured `card_sha256` pre-pin must match or the stage halts. |
| `invoke` | `SendMessage` | `returnImmediately: true`, `acceptedOutputModes: ["application/json"]`. Message: `ROLE_USER`, `contextId` = run id, one `data` part per stage input artifact (`mediaType: application/json`, `filename` = artifact name) plus a context text part. The returned task must preserve that context id; its task id becomes the `InvocationHandle`. |
| `poll` | `GetTask` loop | One-shot snapshots, `poll_seconds` apart, bounded by `timeout_seconds`. Every response must match both the invoked task id and run context id before it is receipted. |
| `collect` | `GetTask` (terminal) | Must observe `TASK_STATE_COMPLETED`. Artifacts map by unique `name` onto the stage's declared outputs; missing, duplicate, or undeclared names halt. Each artifact must carry a JSON part. |

Task state discipline (never guessed):

| `TASK_STATE_…` | Runner terminal |
|---|---|
| `SUBMITTED`, `WORKING` | keep polling |
| `COMPLETED` | stage proceeds |
| `FAILED`, `CANCELED`, `REJECTED` | `failed` |
| `INPUT_REQUIRED`, `AUTH_REQUIRED` | `halted` — "NOT cancelled — operator handoff" |
| `UNSPECIFIED`, anything unparseable | `halted` (ambiguity is never resolved by guessing) |
| timeout elapsed | `halted` — task id + endpoint reported, **the task is never cancelled** |

The runner deliberately has no cancel path: a halted invocation keeps
running on the host and the operator decides. This carries over the old
host-contract discipline verbatim.

## 4. The receipt contracts: `a2a.invocation.v1` and `.v2`

One receipt per wire observation (`schemas/a2a-invocation*.schema.json`,
registered in `src/contracts.rs`). New observations use v2; v1 remains
accepted so previously sealed bundles retain their original meaning:

- `host` — the invocation binding: `endpoint`, `card_url`,
  `card_sha256` (canonical digest of the observed card), `agent`
  (card `name`), `protocol_version`.
- `operation` — `card_discovery` | `send_message` | `get_task`.
- `task_id` + `context_id` + `task_state` — required for task operations, forbidden
  for `card_discovery` (schema-level `if/then`).
- `upstream` — the **verbatim** wire response object, pinned by
  `upstream_sha256` (canonical digest).

Two-layer discipline, carried over from the old host contract:

1. **Wire conformance is the adapter's job.** Upstream payloads are
   validated against the typed model (`model.rs`) before wrapping; an
   unknown task state, a wrong JSON-RPC envelope, or a response that is
   neither task nor message halts before any receipt exists.
2. **Registry validation validates the envelope.** The receipt schema
   deliberately types `upstream` as `{"type": "object"}`: the registry
   cannot know every A2A extension, so it pins the binding fields and the
   digest instead. Re-verification (`stammtisch verify`) re-checks the
   digest chain, which is what makes the embedded payload trustworthy.

Fake and real paths emit the **same** revision — the old design's
`host-receipt.v1` vs `host-invocation.v1` split (two truths for one
stage) is gone. The offline test agent
(`tests/support/fake_a2a.rs`) speaks real A2A wire shapes, so the real
adapter is exercised in the default test suite.

## 5. Fail-closed taxonomy

| Observation | Kind | Outcome |
|---|---|---|
| endpoint not http(s), token env unset/empty | usage | spec/config error (exit 3) |
| connection failure, HTTP error, agent JSON-RPC error | product | stage `failed` (exit 1) |
| card 404 | product | stage `failed` |
| card not JSON, wrong interface, `protocolVersion` mismatch, pre-pin mismatch | integrity | `halted` (exit 2) |
| response not JSON-RPC, RPC id mismatch, task/context binding mismatch, neither result nor error, task/message ambiguity, direct-message reply, unknown task state | integrity | `halted` |
| completed with no artifacts, artifact count ≠ declared outputs, artifact without a JSON part | integrity | `halted` |
| `INPUT_REQUIRED` / `AUTH_REQUIRED` / timeout | — | `halted`, no cancel |

Failure paths keep their evidence: preflight/invoke/poll receipts
accumulate in the adapter and are drained via `drain_receipts` before the
terminal event is recorded.

## 6. Configuration surface

Pipeline stages carry an optional `runtime` block (see
`schemas/pipeline.schema.json`):

```json
"runtime": {
  "protocol": "a2a",
  "endpoint": "https://<agent-jsonrpc>/",
  "card_url": "https://<agent>/.well-known/agent-card.json",
  "token_env": "A2A_TOKEN",
  "card_sha256": "sha256:<64 hex>"
}
```

- `endpoint` is required; `card_url` defaults to the endpoint origin's
  well-known path. `token_env` names an environment variable — credentials
  are never inlined in specs, and a declared-but-unset variable is a hard
  config error.
- `card_sha256` pre-pins the agent identity; a drifted card halts.
- A stage with a `runtime` resolves to the wire adapter regardless of
  product; without one, products resolve to their offline fakes. There is
  **no ambient environment selection** — the old adapter's auto/fake
  fallback is deliberately dropped.

## 7. What changed vs. the old host contract

Kept: per-observation receipts with salvage on failure; digest-bound
bindings (`bin_sha256`/`state_root` → `card_sha256`/`endpoint`);
fail-closed every ambiguity; timeout → halt with run id, never cancel;
exit-preserving error reporting.

Fixed: single receipt revision for fake and real paths; typed wire
validation at the adapter boundary (the old wrapper's free-form inner
receipt); no env-dependent adapter selection; a standard, spec'd wire
format instead of a bespoke CLI envelope.

## 8. Ecosystem notes (researched 2026-08)

- **Adoption.** A2A is Linux Foundation-hosted with 150+ supporting
  organizations. Production integrations include Azure AI Foundry and
  Copilot Studio, AWS Bedrock AgentCore, Salesforce Agentforce, SAP, and
  ServiceNow; documented cross-organization flows (a CRM agent delegating
  contract review to an external legal agent that returns a structured
  verdict) are the same shape as STAMMTISCH product stages.
- **Frameworks.** Google ADK is A2A-native; LangGraph, CrewAI, and
  AutoGen agents are wrapped as A2A servers by mapping graph/crew state
  onto the task lifecycle (interrupts become `INPUT_REQUIRED`). Any of
  them can host a STAMMTISCH product.
- **Rust ecosystem.** `a2a-rs` (`a2a-protocol-sdk`) is a community SDK;
  this adapter deliberately hand-rolls its minimal client (blocking
  `ureq`, no async runtime) to match the core's dependency discipline —
  `serde`/`serde_json`/`sha2`/`ureq` only.
- **The "contract layer" gap.** Protocol surveys (e.g. the Open Standard
  for Software Agents) observe that MCP connects tools and A2A connects
  agents, but neither defines an evidence contract: portable identity,
  binding, and validation rules for what agents *produced*. STAMMTISCH's
  wrapper receipts fill exactly that gap for its own runs — revision-pinned,
  digest-chained, offline-verifiable — which is what makes any conforming
  host interchangeable.

## 9. Deliberate non-goals (current revision)

Streaming (`SendStreamingMessage`/SSE), push notifications, signed Agent
Cards (JWS), `extendedAgentCard`, multi-agent orchestration, and task
cancellation. The poll model makes streaming unnecessary for the
lifecycle; signed cards are redundant with digest pre-pinning; and the
runner never cancels by design. Each maps to a bounded extension of the
adapter, not of the protocol layer.

## Sources

- A2A protocol specification: https://a2a-protocol.org
- A2A project: https://github.com/a2aproject/A2A (spec `a2a.proto`,
  SDKs, samples)
- Linux Foundation: A2A surpasses 150 organizations and enters
  production: https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year
- ServiceNow as an A2A client and server:
  https://www.servicenow.com/community/ceg-ai-coe-articles/servicenow-as-a-primary-a2a-agent-discovering-and-invoking/ta-p/3528579
- Google developer guide to agent protocols:
  https://developers.googleblog.com/developers-guide-to-ai-agent-protocols/
- Open Standard for Software Agents (contract-layer analysis):
  https://openstandardagents.org/research/agent-communication-protocol-survey/
- a2a-rs (Rust SDK): https://docs.rs/crate/a2a-protocol-sdk
