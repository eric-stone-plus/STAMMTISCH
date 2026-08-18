//! Per-run cost ledger (architecture doc §10 P3): folds validated stage
//! receipts plus runner-observed product-contact wall time into
//! `runs/<run-id>/cost.json`, a content-addressed run artifact that ships
//! inside the export bundle.
//!
//! Fail-safe by design: no receipt revision in the pinned contract set
//! carries token usage today, and the ledger records `null` tokens rather
//! than inventing numbers. A ledger failure never blocks a run — the
//! runner ignores [`write_ledger`] errors and bundle assembly ships the
//! ledger only when it exists.

use std::path::Path;

use serde_json::{json, Value};

use crate::error::AppError;
use crate::pipeline::Pipeline;

/// Runner-observed product-contact wall time for one stage, in seconds
/// (from just before adapter `preflight` through `collect`).
#[derive(Debug, Clone)]
pub struct StageTiming {
    pub stage: String,
    pub wall_seconds: f64,
}

/// Token usage recognized from one receipt. Every field is `None` when the
/// receipt carries no usable usage data.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Usage {
    pub input: Option<u64>,
    pub output: Option<u64>,
    pub total: Option<u64>,
}

/// Recognized token-usage convention for wire receipts. The verbatim
/// upstream JSON-RPC result of an `a2a.invocation.v2` receipt may carry a
/// `usage` object with non-negative integer `input_tokens` /
/// `output_tokens` / `total_tokens` fields, either at the result top level
/// (`upstream.usage`) or inside task metadata
/// (`upstream.task.metadata.usage`). No shipped product emits this today;
/// the convention is declared so a future receipt revision can surface
/// usage without a core change. Values that are not non-negative integers
/// are treated as absent — never fabricated.
pub fn extract_usage(receipt: &Value) -> Option<Usage> {
    let upstream = receipt.get("upstream")?;
    read_usage_object(upstream.get("usage")).or_else(|| {
        upstream
            .get("task")
            .and_then(|task| task.get("metadata"))
            .and_then(|metadata| read_usage_object(metadata.get("usage")))
    })
}

fn read_usage_object(value: Option<&Value>) -> Option<Usage> {
    let obj = value?.as_object()?;
    let usage = Usage {
        input: u64_field(obj, "input_tokens"),
        output: u64_field(obj, "output_tokens"),
        total: u64_field(obj, "total_tokens"),
    };
    (usage.input.is_some() || usage.output.is_some() || usage.total.is_some()).then_some(usage)
}

fn u64_field(obj: &serde_json::Map<String, Value>, key: &str) -> Option<u64> {
    obj.get(key).and_then(Value::as_u64)
}

fn add_opt(a: Option<u64>, b: Option<u64>) -> Option<u64> {
    match (a, b) {
        (Some(x), Some(y)) => Some(x.saturating_add(y)),
        (Some(x), None) => Some(x),
        (None, Some(y)) => Some(y),
        (None, None) => None,
    }
}

/// Build the ledger document for a run from its accepted receipts on disk
/// and the runner-observed timings. Stage order follows pipeline order.
pub fn build(
    run_dir: &Path,
    run_id: &str,
    pipeline: &Pipeline,
    timings: &[StageTiming],
) -> Result<Value, AppError> {
    let mut stages = Vec::new();
    for stage in &pipeline.stages {
        let files = stage_receipt_files(run_dir, &stage.id)?;
        // Distinct task ids count as invocations for wire products (one
        // task = one product invocation despite many wire observations);
        // CLI/fake products emit exactly one receipt per invocation.
        let mut distinct_tasks = std::collections::BTreeSet::new();
        let mut saw_task_receipt = false;
        let mut tokens = Usage::default();
        let mut usage_receipts: Vec<String> = Vec::new();
        for rel in &files {
            let bytes = std::fs::read(run_dir.join(rel)).map_err(|e| {
                AppError::integrity(
                    "cost_receipt_unreadable",
                    format!("cost ledger cannot read {rel}: {e}"),
                )
            })?;
            let receipt: Value = serde_json::from_slice(&bytes).map_err(|e| {
                AppError::integrity(
                    "cost_receipt_unparseable",
                    format!("cost ledger cannot parse {rel}: {e}"),
                )
            })?;
            let revision = receipt.get("schema").and_then(Value::as_str).unwrap_or("");
            if matches!(revision, "a2a.invocation.v1" | "a2a.invocation.v2") {
                if let Some(task_id) = receipt.get("task_id").and_then(Value::as_str) {
                    saw_task_receipt = true;
                    distinct_tasks.insert(task_id.to_string());
                }
            }
            if let Some(usage) = extract_usage(&receipt) {
                tokens.input = add_opt(tokens.input, usage.input);
                tokens.output = add_opt(tokens.output, usage.output);
                tokens.total = add_opt(tokens.total, usage.total);
                usage_receipts.push(rel.clone());
            }
        }
        let invocations = if saw_task_receipt {
            distinct_tasks.len()
        } else {
            files.len()
        };
        let wall_seconds = timings
            .iter()
            .find(|t| t.stage == stage.id)
            .map(|t| t.wall_seconds)
            .unwrap_or(0.0);
        stages.push(json!({
            "stage": stage.id,
            "product": stage.product,
            "invocations": invocations,
            "observations": files.len(),
            "wall_seconds": wall_seconds,
            "tokens": {
                "input": tokens.input,
                "output": tokens.output,
                "total": tokens.total,
            },
            "usage_receipts": usage_receipts,
        }));
    }

    let ledger = json!({
        "schema": "stammtisch.cost-ledger.v0",
        "run_id": run_id,
        "pipeline_id": pipeline.id,
        "stages": stages,
        "generated_at": crate::time::now_rfc3339(),
    });
    let schema: Value =
        serde_json::from_str(crate::schemas::COST_LEDGER).expect("embedded schema parses");
    let errs = crate::jsonval::violations(&schema, &ledger);
    if !errs.is_empty() {
        return Err(AppError::internal(format!(
            "cost ledger violates its schema: {}",
            errs.join("; ")
        )));
    }
    Ok(ledger)
}

/// Best-effort write of `runs/<run-id>/cost.json`. Callers (the runner)
/// must ignore failures: cost accounting never blocks a run.
pub fn write_ledger(
    run_dir: &Path,
    run_id: &str,
    pipeline: &Pipeline,
    timings: &[StageTiming],
) -> Result<(), AppError> {
    let ledger = build(run_dir, run_id, pipeline, timings)?;
    crate::store::atomic_write(&run_dir.join("cost.json"), &crate::canon::canonical_bytes(&ledger))
}

/// All accepted receipt files for one stage, sorted (`receipts/<stage>.<n>.json`).
fn stage_receipt_files(run_dir: &Path, stage: &str) -> Result<Vec<String>, AppError> {
    let prefix = format!("{stage}.");
    let dir = run_dir.join("receipts");
    let mut names: Vec<String> = Vec::new();
    for entry in std::fs::read_dir(&dir)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with(&prefix) && name.ends_with(".json") {
            names.push(name);
        }
    }
    names.sort();
    Ok(names.into_iter().map(|name| format!("receipts/{name}")).collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ids;
    use crate::pipeline;
    use std::path::PathBuf;

    fn fixture_dir(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("stammtisch-cost-{tag}-{}", ids::uuid_v7().unwrap()));
        std::fs::create_dir_all(dir.join("receipts")).unwrap();
        dir
    }

    fn write_receipt(dir: &Path, rel: &str, receipt: &Value) {
        std::fs::write(
            dir.join("receipts").join(rel),
            crate::canon::canonical_bytes(receipt),
        )
        .unwrap();
    }

    fn pipeline_fixture() -> Pipeline {
        let v = json!({
            "schema": "stammtisch.pipeline.v0",
            "id": "t-cost",
            "doctrine": {"pack": "galahad"},
            "stages": [
                {"id": "brief", "product": "doctrine", "out": ["brief.json"]},
                {"id": "review", "product": "quinte",
                 "runtime": {"protocol": "a2a", "endpoint": "https://a2a.example.invalid/"}},
                {"id": "deliver", "product": "highball", "adapter": "fake",
                 "in": ["brief.json"], "out": ["deliver.packet.json"]}
            ]
        });
        pipeline::validate(&v, Path::new("x.json")).unwrap()
    }

    fn a2a_receipt(task_id: &str, operation: &str, usage: Option<Value>) -> Value {
        let upstream = match usage {
            Some(u) => json!({"task": {"id": task_id, "status": {"state": "TASK_STATE_COMPLETED"}, "metadata": {}}, "usage": u}),
            None => json!({"task": {"id": task_id, "status": {"state": "TASK_STATE_COMPLETED"}}}),
        };
        json!({
            "schema": "a2a.invocation.v2",
            "host": {
                "endpoint": "https://a2a.example.invalid/",
                "card_url": "https://a2a.example.invalid/.well-known/agent-card.json",
                "card_sha256": format!("sha256:{}", "1".repeat(64)),
                "agent": "test-agent",
                "protocol_version": "1.0"
            },
            "stage": "review",
            "operation": operation,
            "observed_at": "2026-08-18T00:00:00.000Z",
            "invocation_id": ids::uuid_v7().unwrap(),
            "task_id": task_id,
            "context_id": "run-1",
            "task_state": "TASK_STATE_COMPLETED",
            "upstream": upstream,
            "upstream_sha256": crate::canon::sha256_value_prefixed(&upstream)
        })
    }

    fn doctrine_receipt() -> Value {
        json!({
            "schema": "doctrine.brief.v0",
            "pack": "galahad",
            "pack_sha256": format!("sha256:{}", "2".repeat(64)),
            "brief_sha256": format!("sha256:{}", "3".repeat(64))
        })
    }

    #[test]
    fn extract_usage_reads_upstream_usage_and_task_metadata() {
        let receipt = a2a_receipt("t1", "get_task", Some(json!({
            "input_tokens": 12, "output_tokens": 34, "total_tokens": 46
        })));
        let usage = extract_usage(&receipt).expect("usage recognized");
        assert_eq!(
            usage,
            Usage { input: Some(12), output: Some(34), total: Some(46) }
        );

        // Task metadata path.
        let mut via_metadata = receipt.clone();
        via_metadata["upstream"] = json!({
            "task": {"id": "t1", "metadata": {
                "usage": {"input_tokens": 5, "output_tokens": 6}
            }}
        });
        let usage = extract_usage(&via_metadata).expect("metadata usage recognized");
        assert_eq!(usage, Usage { input: Some(5), output: Some(6), total: None });
    }

    #[test]
    fn extract_usage_is_null_when_absent_or_garbage() {
        // No usage anywhere.
        assert!(extract_usage(&a2a_receipt("t1", "get_task", None)).is_none());
        // Non-integer and negative values are treated as absent.
        let mut bad = a2a_receipt("t1", "get_task", None);
        bad["upstream"]["usage"] = json!({
            "input_tokens": "many", "output_tokens": -3, "total_tokens": true
        });
        assert!(extract_usage(&bad).is_none());
        // CLI receipts have no upstream container at all.
        assert!(extract_usage(&doctrine_receipt()).is_none());
    }

    #[test]
    fn ledger_aggregates_invocations_wall_time_and_tokens() {
        let dir = fixture_dir("aggregate");
        let run_id = ids::uuid_v7().unwrap();
        write_receipt(&dir, "brief.0.json", &doctrine_receipt());
        // One invocation = one distinct task id despite several receipts.
        write_receipt(&dir, "review.0.json", &a2a_receipt("task-9", "send_message", None));
        write_receipt(&dir, "review.1.json", &a2a_receipt("task-9", "get_task", Some(json!({
            "input_tokens": 100, "output_tokens": 200, "total_tokens": 300
        }))));
        write_receipt(&dir, "review.2.json", &a2a_receipt("task-9", "get_task", Some(json!({
            "input_tokens": 10, "output_tokens": 20
        }))));
        write_receipt(&dir, "deliver.0.json", &json!({
            "schema": "highball.action-packet.v1",
            "packet_id": "p1", "route": "direct-evidence",
            "decision": "AUTHORIZED", "action_decision": "pass"
        }));
        let pipeline = pipeline_fixture();
        let timings = [
            StageTiming { stage: "brief".into(), wall_seconds: 0.001 },
            StageTiming { stage: "review".into(), wall_seconds: 12.5 },
            StageTiming { stage: "deliver".into(), wall_seconds: 0.002 },
        ];
        let ledger = build(&dir, &run_id, &pipeline, &timings).unwrap();
        assert_eq!(ledger["run_id"], run_id);
        assert_eq!(ledger["pipeline_id"], "t-cost");

        let stages = ledger["stages"].as_array().unwrap();
        assert_eq!(stages.len(), 3, "every stage accounted in pipeline order");

        // Wire stage: one invocation, four observations, summed tokens.
        let review = &stages[1];
        assert_eq!(review["stage"], "review");
        assert_eq!(review["invocations"], 1);
        assert_eq!(review["observations"], 3);
        assert_eq!(review["wall_seconds"], 12.5);
        assert_eq!(review["tokens"]["input"], 110);
        assert_eq!(review["tokens"]["output"], 220);
        assert_eq!(review["tokens"]["total"], 300);
        assert_eq!(
            review["usage_receipts"],
            json!(["receipts/review.1.json", "receipts/review.2.json"])
        );

        // CLI stages: one receipt per invocation, tokens unknown (null).
        assert_eq!(stages[0]["invocations"], 1);
        assert_eq!(stages[0]["observations"], 1);
        assert_eq!(stages[0]["tokens"]["input"], Value::Null);
        assert_eq!(stages[0]["tokens"]["output"], Value::Null);
        assert_eq!(stages[0]["tokens"]["total"], Value::Null);
        assert_eq!(stages[0]["usage_receipts"], json!([]));
        assert_eq!(stages[2]["invocations"], 1);
        assert_eq!(stages[2]["tokens"]["input"], Value::Null);

        // The ledger validates against its own contract.
        let schema: Value = serde_json::from_str(crate::schemas::COST_LEDGER).unwrap();
        let errs = crate::jsonval::violations(&schema, &ledger);
        assert!(errs.is_empty(), "ledger violates schema: {errs:?}");

        // write_ledger round-trips to cost.json.
        write_ledger(&dir, &run_id, &pipeline, &timings).unwrap();
        assert!(dir.join("cost.json").is_file());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn ledger_records_distinct_task_ids_as_invocations() {
        let dir = fixture_dir("tasks");
        let run_id = ids::uuid_v7().unwrap();
        // Two tasks observed across five receipts.
        write_receipt(&dir, "review.0.json", &a2a_receipt("task-a", "send_message", None));
        write_receipt(&dir, "review.1.json", &a2a_receipt("task-a", "get_task", None));
        write_receipt(&dir, "review.2.json", &a2a_receipt("task-b", "send_message", None));
        write_receipt(&dir, "review.3.json", &a2a_receipt("task-b", "get_task", None));
        // A card-discovery observation is not an invocation.
        let mut card = a2a_receipt("task-a", "card_discovery", None);
        card.as_object_mut().unwrap().remove("task_id");
        card.as_object_mut().unwrap().remove("context_id");
        card.as_object_mut().unwrap().remove("task_state");
        card["upstream"] = json!({"capabilities": {"streaming": false}});
        card["upstream_sha256"] =
            json!(crate::canon::sha256_value_prefixed(&card["upstream"]));
        write_receipt(&dir, "review.4.json", &card);
        let pipeline = pipeline_fixture();
        let ledger = build(&dir, &run_id, &pipeline, &[]).unwrap();
        let review = &ledger["stages"].as_array().unwrap()[1];
        assert_eq!(review["invocations"], 2);
        assert_eq!(review["observations"], 5);
        assert_eq!(review["wall_seconds"], 0.0, "missing timing records zero, never a guess");
        std::fs::remove_dir_all(&dir).ok();
    }
}
