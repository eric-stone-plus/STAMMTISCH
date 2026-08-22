# Full-stack quickstart — reproducing your own STAMMTISCH from the six public repos

This guide walks a stranger through standing up the complete quant
workstation: STAMMTISCH (the workstation), GALAHAD (its analysis
identity), QUINTE (the review orchestrator), HIGHBALL (the delivery
rules plane), RASHOMON (the philosophy), and CAUSEWAY (optional
egress). At the end you run one pipeline end to end: brief → five-school
review → authorization, with offline-verifiable evidence.

## Reproducibility boundary (read this first)

The public repositories carry **mechanisms, contracts, and synthetic
fixtures only**. Everything here reproduces with `cargo build` /
`pytest` on a clean checkout, with no credentials and no network beyond
what each step declares.

What is deliberately NOT public, and cannot be reproduced from these
repos:

- **Private strategies, research briefs, and doctrine content.** The
  shipped doctrine pack (`doctrine/examples/galahad`) is an example
  fixture with an intentionally empty brief template. Your own
  strategies and doctrine packs are your inputs: the stack runs them,
  the repos do not contain them.
- **API keys and credentials.** Everything is read from named
  environment variables (below); nothing is stored in a repo.
- **Data caches and venue history.** Fixtures are synthetic. Your
  live-data caches live in local state roots, never in git.
- **Private archives** (retired product histories and the private
  research workspace) are out of scope entirely.

So: a stranger can precisely reproduce the *system*. The *content* it
analyzes — strategies, briefs, doctrine — is authored separately, which
is exactly the product boundary.

## One-click vs. full stack

The one-click bootstrap
(`scripts/bootstrap-fullstack.sh`, see the README) installs the
**STAMMTISCH base only**. The review orchestrator, the rules plane, the
analysis identity, and the philosophy are separate repositories and are
installed deliberately with the steps below — the boundary is
intentional: the base platform is the entry point; the rest of the
stack is opt-in.

## Prerequisites

- Rust (stable), Python 3.11+ with `uv` for the TUI, and a DeepSeek API
  key (`DEEPSEEK_API_KEY`, from platform.deepseek.com) — the review
  seats run on it.
- Optional but recommended: the GALAHAD repo for the analysis surface
  and the parity evidence tooling.

## Build order

```sh
# 1. STAMMTISCH (workstation)
git clone https://github.com/eric-stone-plus/STAMMTISCH && cd STAMMTISCH
cargo build --release            # stammtisch-core

# 2. QUINTE (review orchestrator)
git clone https://github.com/eric-stone-plus/QUINTE && cd QUINTE
cargo build --release            # quinte (host serve + run machinery)
# the PI seat agent crate lives in QUINTE/pi/ — optional for the default
# in-process DeepSeek seats, required for external-seat topologies

# 3. HIGHBALL (delivery rules plane)
git clone https://github.com/eric-stone-plus/HIGHBALL && cd HIGHBALL
cargo build --release            # highball + build-action-packet

# 4. GALAHAD (analysis identity + evidence tooling) — optional but recommended
git clone https://github.com/eric-stone-plus/GALAHAD && cd GALAHAD/galahad-futures
uv venv .venv && uv pip install --python .venv/bin/python -e . pytest
.venv/bin/python -m pytest tests/ -q
```

RASHOMON is documentation only (clone to read). CAUSEWAY is an optional
egress gateway for research data fetching; it is not on the pipeline
path.

## Environment (the complete set for one run)

```sh
# QUINTE seats (in-process DeepSeek). All four are required by
# preflight — each missing one is reported individually:
export DEEPSEEK_API_KEY=sk-...                       # your key
export DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
export QUINTE_PROVIDER_KEY_ENV=DEEPSEEK_API_KEY      # selector: which var
export QUINTE_PROVIDER_BASE_URL_ENV=DEEPSEEK_BASE_URL
# Wire token shared by QUINTE server and the STAMMTISCH runtime block:
export A2A_TOKEN=<your-choice>
# HIGHBALL product binary for the deliver stage:
export HIGHBALL_BIN=/path/to/HIGHBALL/target/release/build-action-packet
# QUINTE binary that produced the review run — HIGHBALL verifies its
# digest against the run manifest (default: $HOME/.cargo/bin/quinte):
export HIGHBALL_QUINTE_BIN=$HOME/.cargo/bin/quinte
# State roots (anywhere you like):
export STAMMTISCH_HOME=$HOME/.local/share/stammtisch
export QUINTE_HOME=$HOME/.local/share/quinte
```

## Run

```sh
# Initialize and start the QUINTE A2A server (loopback + bearer token):
cd QUINTE && ./target/release/quinte init && \
  ./target/release/quinte host serve --bind 127.0.0.1:8802 --token-env A2A_TOKEN --json

# In another shell: initialize and run the full-stack pipeline.
cd STAMMTISCH && stammtisch init
stammtisch run --pipeline pipelines/examples/fullstack.json
stammtisch status
```

What happens: the doctrine stage renders the example brief; the review
stage sends it to QUINTE, which runs the five-school R1 (contested-only
R2, dual-arbiter R3) and returns `review.result` (Result 2.1 with the
same-model `trial_manifest` caveat) plus the two HIGHBALL carriers; the
deliver stage hands the carriers to HIGHBALL for an Action Packet.

**Expected terminal state: `blocked`.** The shipped example brief is
intentionally empty ("no candidate strategy"), so all five schools
refuse it and HIGHBALL's authorization verdict is `block`. A blocked run
is success-shaped: the pipeline, the review, and the authorization
machinery all worked — the evidence says why it must not ship. To get a
`completed` run, author your own doctrine pack with a real brief
template and acceptance gates; the stack does not change.

## Inspect and verify offline

```sh
stammtisch inspect <RUN_ID>       # receipts, gates, verdicts
stammtisch export <RUN_ID> --out /tmp/bundle
stammtisch verify --bundle /tmp/bundle   # re-verifies everything offline
```

The review gate record carries the honest-labeling evidence
(`base_model_relation`, `contamination_risks`) and the result's
`observed_contestation` reports how much the five schools actually
diverged.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `-32012 … preflight failed` | one of the four DeepSeek env vars above is missing — `quinte host preflight --json` names it |
| `-32010 busy_run` | another review is already running on this QUINTE home; wait or use a separate `QUINTE_HOME` |
| deliver stage: `product_cli_missing` | `HIGHBALL_BIN` unset or not pointing at `build-action-packet` |
| deliver stage: `highball_quinte_run_missing` / `highball_quinte_result_missing` | the review.result artifact has no `run_id`, or `$QUINTE_HOME/runs/<run_id>/result.json` is gone |
| deliver stage: `highball_quinte_bin_missing` | `HIGHBALL_QUINTE_BIN` unset and no `$HOME/.cargo/bin/quinte`; it must be the exact binary that produced the run |
| `doctrine_not_found` | run from the repo or keep the example's relative layout; a pipeline outside the repo must declare `doctrine.ref` |
| port 8802 in use | pick another port and update the example's `runtime.endpoint` |
