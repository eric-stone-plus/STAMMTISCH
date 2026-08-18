# RASHOMON: how STAMMTISCH interprets and responds to the five gates

RASHOMON (the multi-perspective residual architecture) states epistemic
requirements; this document fixes where each requirement is enforced in
the STAMMTISCH stack. The mapping is normative: a change to an
enforcement point updates this table and the RASHOMON-side mapping in
the same change set.

| RASHOMON gate | Requirement | STAMMTISCH-side enforcement |
| --- | --- | --- |
| **Amamon** (ambiguity) | An unclear question must `clarify()`, never guess | The A2A adapter maps an `INPUT_REQUIRED` task to a stage halt; the halt detail carries the task id and endpoint for operator handoff. The runner never answers on the product's behalf. |
| **Kyōmon** (mirror) | Comparative claims must be evidence-anchored | QUINTE lane validation: every claim/residual `evidence_refs` must resolve to the run's allowed evidence set (unknown refs fail the lane); `task_restatement` is schema-required; cross-seat attestation symmetry (majority rule) is the `r1_contestation` predicate feeding R2. On the host side, every artifact a gate reads is digest-bound; gate records cite the digest they evaluated. |
| **Shōmon** (testimony) | Perspectives must confront each other; residuals are the material | The QUINTE review stage: R1 five schools (PI seat agent, Party A–E), contested-only R2 (durable k=0 skip on unanimity), dual R3 arbitration. The `quinte_result` gate accepts Result 2.1 and records `residual_count`, the model relation, and contamination risks as observed evidence. |
| **Kan'nukimon** (anti-drift) | Prompts must resist contamination; drift caught in the first line | PI owns only its school role text (task packet first); every lane output restates the task in a schema-required `task_restatement` field — drift is a schema violation, not a soft spot. |
| **Kennōmon** (architecture gate) | The constitution cannot be rewritten by the constrained | HIGHBALL's Protected-Write Guard requires a valid verdict trail (with residual closure) before protected writes; STAMMTISCH's `packet_authorized` gate refuses a deliver stage whose Action Packet 2.0 is missing, malformed, or `review`/`block`. |

## Honest labeling is part of the host contract

The `quinte_result` gate now **requires** `trial_manifest.base_model_relation`
(a non-empty string) — a Result 2.1 that cannot attest how its perspectives
relate fails the gate. The same-model caveat
(`base_model_relation`, `perspective_count`, `contamination_risks`) is
copied into the gate record's observed evidence, so the limitation travels
with every run's audit trail instead of being silently dropped.

## Why STAMMTISCH is the responding party

RASHOMON asks the prior question (why must perspectives confront each
other); QUINTE, HIGHBALL, and GALAHAD instantiate the answers; STAMMTISCH
is the place where the answers are *provable* — every gate evaluation is a
deterministic record over digest-bound artifacts, re-verifiable offline
from the exported bundle. A RASHOMON claim about a run is checkable, not
narrative.

## Parity evidence wiring

The GALAHAD dual-engine parity report (schema `galahad.parity.v1`,
`run_parity.py --json`) is research evidence for the review stages.
The operator runs it before a pipeline run, then declares the stable
pointer as brief-stage evidence:

```jsonc
{ "id": "brief", "product": "doctrine", "out": ["brief.json"],
  "evidence": ["/path/to/galahad-futures/output/parity_last.json"] }
```

The brief renderer copies the declared path into the brief's
`evidence_roots`; the QUINTE review projects those roots into the seat
packets, so the five schools see the engine reconciliation (boundary
crossings, threshold sensitivity) as digest-bound evidence — never as
an ambient file. The parity report itself is not a promotion gate: the
reference book remains the arbiter.
