# Daily Data and Report Pipeline

STAMMTISCH treats the daily report as a downstream view of accepted market
data. The `D` surface starts the configured intake product and verifies its
artifacts before presenting either records or the report.

The required derivation order is:

```text
Firecrawl capture
  -> append-only evidence manifest
  -> deterministic quality checks
  -> canonical dataset
  -> report JSON
  -> report HTML
```

Report JSON and HTML must never become source inputs for the canonical
dataset. Every transition is bound by SHA-256 metadata, and STAMMTISCH checks
the evidence files, source and market counts, canonical record identities,
and downstream lineage before accepting a workspace.

## Artifact roles

- **Firecrawl capture:** the configured product collects each declared source.
  Every non-empty response is retained as immutable raw evidence, including
  responses that later fail or are pruned by a quality gate.
- **Evidence manifest:** assigns exactly one terminal state to each expected
  source: `succeeded`, `failed`, or `pruned`. Successful captures bind their
  evidence path, byte count, and SHA-256 digest.
- **Quality checks:** reject malformed responses, weak captures, missing
  evidence, digest drift, inconsistent counts, unsupported contracts, and
  records whose URLs are absent from their cited evidence. Failures are
  visible; they are not silently promoted into the dataset.
- **Canonical dataset:** contains the accepted records and is the sole input
  to report production. Record identity, market, source, title, URL, and
  evidence references remain fixed downstream.
- **Report JSON:** adds the Chinese editorial layer without changing canonical
  source identity. The configured DS Pro report builder runs only after the
  canonical dataset has been materialized from accepted evidence. A
  deterministic builder is available for offline operation and fallback.
- **Report HTML:** is rendered only from report JSON and embeds the input JSON
  digest for verification.

Raw evidence is append-only. A new capture creates a new evidence object and
run directory; it does not overwrite an earlier response or accepted run.
Bounded retries retain every non-empty attempt. A rejected session stops before
DS Pro and publishes no report JSON or HTML; `D` may show its verified evidence
diagnostics, but it cannot open them as a report.

## Independent market sessions

A-share, Hong Kong, US, Japan, Korea, Singapore, and crypto sessions are not
one coupled quality gate. Each market is captured, evidenced, and accepted on
its own exchange timeline. A downstream assembly may combine the latest
accepted slices available at its reading cutoff; one rejected slice remains
visible but cannot invalidate another market's accepted evidence.

The product may expose a market selector and an explicit assembly mode. Public
STAMMTISCH does not encode exchange hours or assume that every market has
closed at the same wall-clock time. A market that has not closed must be marked
as an intraday/as-of slice, while its latest completed session remains a
separate baseline.

Revision 1 session metadata is verified against an offline calendar identity:
`exchange_calendars` version `4.13.2` with `XSHG`, `XHKG`, `XNYS`, `XTKS`,
`XKRX`, or `XSES`, while crypto uses the built-in `24/7` calendar. The product
records actual local and UTC open/close times, adjacent certified sessions, and
the calendar availability state. Missing, out-of-range, or failed calendar data
must remain partial and cannot be replaced by a weekday or fixed-clock guess.
`calendar_completeness` describes the exchange cutoff; `completeness` separately
describes publication readiness after source quality checks.

## Language policy

The daily report is Chinese. This includes editorial summaries and report
navigation intended for the reader.

STAMMTISCH framework text remains English: configuration names, statuses,
errors, contracts, workspace labels, and the `D` interface do not introduce
Chinese tool text. Source material is not framework text and is preserved in
its original language. In particular, English headlines remain English and
Chinese source content remains Chinese. The report builder may add a Chinese
summary, but it must not replace or translate the source title.

## Scope boundaries

Daily intake establishes trustworthy data. It does not perform sentiment
analysis, generate investment recommendations, place orders, or enable any
form of automated trading. Sentiment remains on the separate `S` surface.
Recommendation and paper-trading workflows may consume an accepted canonical
dataset later, but they are not intake gates and cannot alter intake evidence.

## Local and device-neutral operation

All evidence, datasets, and report artifacts stay in the configured local
workspace. The pipeline has no OneDrive or other cloud-drive synchronization
step.

The public repository does not embed a machine path, Firecrawl host, port,
proxy layout, or product checkout. Each installation supplies:

- `intake_cmd`: a tokenized daily-data product command;
- `workspace_root`: the local destination for evidence and run artifacts;
- `intake_timeout_seconds`: the bounded product timeout;
- `intake_report_builder`: `deepseek` or `deterministic`;
- any Firecrawl endpoint, credentials, and source configuration required by
  the selected product.

The equivalent environment overrides are `STAMMTISCH_INTAKE_CMD` and
`STAMMTISCH_WORKSPACE_ROOT`. STAMMTISCH launches the command without a shell,
adds `--workspace-root`, `--json`, and the optional report date, then validates
the returned `stammtisch.daily-intake.v1` envelope. The product owns capture
transport; the public TUI owns contract and artifact verification.

See [the TUI guide](../../tui/README.md) for configuration and interaction
details.

## Scheduling status

Scheduling is deliberately deferred. No cron, systemd timer, or
device-specific background job is part of this repository or installed by the
`D` workflow. Capture currently starts on explicit user action. A future
scheduler must remain an external, replaceable trigger for the same intake
contract and must use per-exchange holiday, half-day, emergency-closure, and
daylight-saving rules. There is no universal 17:20 Asia/Shanghai capture time:
an external scheduler derives each trigger from that exchange's close and
publication lag, plus the user's reading deadline. It must not be tied to one
computer.
