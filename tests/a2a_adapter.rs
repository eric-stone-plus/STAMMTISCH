//! A2A adapter end-to-end tests (offline, in-process): the real adapter
//! drives the scripted fake A2A agent in `support/fake_a2a.rs` through the
//! full runner state machine. Real-host integration stays env-gated in
//! `a2a_real_it.rs` (AGENTS.md rule 6).
//!
//! Coverage: the happy task lifecycle (card → send → poll → collect), the
//! fail-closed halts (input-required, timeout, card pin mismatch, protocol
//! mismatch, direct-message reply, garbage wire, artifact contract
//! violations), and the salvage discipline (every failure path keeps its
//! wire receipts).

mod support;

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use stammtisch::canon;
use stammtisch::contracts;
use stammtisch::runner::{run_pipeline, Terminal};
use stammtisch::store::StateRoot;
use support::fake_a2a::{FakeA2a, Script};

/// Wall-clock nanoseconds alone collided across parallel test threads on
/// macOS CI (coarse clock): two tests shared one state root, and the
/// loser's Drop deleted the winner's run mid-flight. Pid + an atomic
/// sequence number make the suffix unique by construction.
fn unique_suffix() -> String {
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    format!(
        "{nanos}-{}-{}",
        std::process::id(),
        SEQ.fetch_add(1, Ordering::SeqCst)
    )
}

struct Tmp(PathBuf);

impl Tmp {
    fn new() -> Self {
        let dir = std::env::temp_dir().join(format!("stammtisch-a2a-{}", unique_suffix()));
        fs::create_dir_all(&dir).unwrap();
        Self(dir)
    }
    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for Tmp {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).ok();
    }
}

/// galahad pack copy + state root + pipeline spec wiring the fake agent.
fn prepare(tmp: &Tmp, server: &FakeA2a, review_overrides: Value) -> (StateRoot, PathBuf) {
    let root = StateRoot {
        path: tmp.path().join("home"),
    };
    root.init().unwrap();

    let pack_src = Path::new(env!("CARGO_MANIFEST_DIR")).join("doctrine/examples/galahad");
    copy_dir(&pack_src, &tmp.path().join("doctrine/galahad"));

    let mut review = json!({
        "id": "review",
        "product": "quinte",
        "in": ["brief.json"],
        "out": ["review.result"],
        "timeout_seconds": 30,
        "poll_seconds": 1,
        "runtime": {
            "protocol": "a2a",
            "endpoint": server.endpoint,
            "card_url": server.card_url,
        }
    });
    merge(&mut review, &review_overrides);
    let spec = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": "a2a-e2e",
        "doctrine": {"pack": "galahad"},
        "stages": [
            {"id": "brief", "product": "doctrine", "out": ["brief.json"],
             "gate": "brief_schema_valid"},
            review,
        ]
    });
    let pipeline_path = tmp.path().join("pipeline.json");
    fs::write(&pipeline_path, serde_json::to_string_pretty(&spec).unwrap()).unwrap();
    (root, pipeline_path)
}

fn merge(target: &mut Value, patch: &Value) {
    if let (Some(t), Some(p)) = (target.as_object_mut(), patch.as_object()) {
        for (k, v) in p {
            t.insert(k.clone(), v.clone());
        }
    }
}

fn copy_dir(src: &Path, dst: &Path) {
    fs::create_dir_all(dst).unwrap();
    for entry in fs::read_dir(src).unwrap() {
        let entry = entry.unwrap();
        let to = dst.join(entry.file_name());
        if entry.path().is_dir() {
            copy_dir(&entry.path(), &to);
        } else {
            fs::copy(entry.path(), &to).unwrap();
        }
    }
}

/// Receipts persisted for one stage, in order.
fn stage_receipts(run_dir: &Path, stage: &str) -> Vec<Value> {
    let mut out = Vec::new();
    let mut n = 0;
    loop {
        let p = run_dir.join("receipts").join(format!("{stage}.{n}.json"));
        if !p.exists() {
            break;
        }
        let bytes = fs::read(&p).unwrap();
        // Every persisted receipt is contract-valid by construction; the
        // runner rejects anything else. Re-validate to keep the invariant
        // explicit in these tests.
        contracts::validate_receipt(&bytes).unwrap();
        out.push(serde_json::from_slice(&bytes).unwrap());
        n += 1;
    }
    out
}

fn states(states: &[&str]) -> Vec<String> {
    states.iter().map(|s| s.to_string()).collect()
}

fn review_result_artifact() -> Value {
    // Representative QUINTE reply: the wire product emits Result 2.1,
    // not the legacy deterministic galahad.review-result.v0 shape.
    json!({
        "artifactId": "a1",
        "name": "review.result",
        "parts": [{
            "data": {
                "result_version": "2.1",
                "run_id": "q-run-1",
                "status": "completed",
                "brief_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
                "question": "representative review question",
                "action_scope": null,
                "affected_paths": [],
                "action_binding_sha256": null,
                "seat_binding": {"seats": []},
                "route_bindings": [],
                "summary": "representative summary",
                "recommendation": "proceed",
                "dissent": [],
                "residuals": [],
                "trial_manifest": {
                    "base_model_relation": "same_model",
                    "perspective_count": 5,
                    "contamination_risks": ["same-family: not independent confirmation"]
                }
            },
            "mediaType": "application/json"
        }]
    })
}

fn named_artifact(id: &str, name: &str, marker: &str) -> Value {
    json!({
        "artifactId": id,
        "name": name,
        "parts": [{
            "data": {"schema": "test.named.v1", "marker": marker},
            "mediaType": "application/json"
        }]
    })
}

#[test]
fn happy_path_completes_with_contract_artifacts() {
    let server = FakeA2a::start(Script {
        poll_states: states(&[
            "TASK_STATE_SUBMITTED",
            "TASK_STATE_WORKING",
            "TASK_STATE_COMPLETED",
        ]),
        artifacts: vec![review_result_artifact()],
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Completed);
    let run_dir = root.run_dir(&report.run_id);

    // The wire trail: card_discovery → send_message → get_task … (terminal
    // snapshot last), all contract-valid and digest-bound.
    let receipts = stage_receipts(&run_dir, "review");
    assert_eq!(receipts[0]["operation"], "card_discovery");
    assert_eq!(receipts[1]["operation"], "send_message");
    let last = receipts.last().unwrap();
    assert_eq!(last["operation"], "get_task");
    assert_eq!(last["task_state"], "TASK_STATE_COMPLETED");
    assert!(last["task_id"].as_str().unwrap().starts_with("task-"));
    for r in &receipts {
        // upstream_sha256 binds the verbatim wire object.
        let upstream = &r["upstream"];
        assert_eq!(r["upstream_sha256"], canon::sha256_value_prefixed(upstream));
        // every receipt is pinned to the same host binding.
        assert_eq!(r["host"]["card_sha256"], receipts[0]["host"]["card_sha256"]);
        assert_eq!(r["host"]["endpoint"], server.endpoint.as_str());
    }

    // The stage's declared output materializes content-addressed.
    let manifest = stammtisch::runner::load_manifest(&run_dir).unwrap();
    let artifact_digest = manifest["stages"][1]["artifacts"][0]
        .as_str()
        .unwrap()
        .to_string();
    let hex = artifact_digest.strip_prefix("sha256:").unwrap();
    let stored: Value =
        serde_json::from_slice(&fs::read(run_dir.join("artifacts").join(hex)).unwrap()).unwrap();
    assert_eq!(stored["result_version"], json!("2.1"));
    assert_eq!(
        stored["trial_manifest"]["base_model_relation"],
        json!("same_model")
    );

    // The invocation message carried the stage's input artifacts as JSON
    // data parts — the adapter is product-agnostic.
    let msg = server.last_message();
    assert_eq!(msg["role"], "ROLE_USER");
    let brief_part = msg["parts"]
        .as_array()
        .unwrap()
        .iter()
        .find(|p| p["filename"] == "brief.json")
        .expect("brief.json data part");
    assert_eq!(brief_part["mediaType"], "application/json");
    assert!(brief_part["data"]["objectives"].is_array());
}

#[test]
fn invocation_sends_only_declared_inputs() {
    let server = FakeA2a::start(Script {
        poll_states: states(&["TASK_STATE_COMPLETED"]),
        artifacts: vec![review_result_artifact()],
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    // The runner unit test exercises a produced map containing both declared
    // and undeclared history. At this wire boundary, exactly stage.in must be
    // represented by JSON data parts.
    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Completed);
    let message = server.last_message();
    let filenames: Vec<&str> = message["parts"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|part| part.get("filename").and_then(Value::as_str))
        .collect();
    assert_eq!(filenames, vec!["brief.json"]);
}

#[test]
fn mismatched_task_id_halts_before_accepting_poll_receipt() {
    let server = FakeA2a::start(Script {
        poll_states: states(&["TASK_STATE_COMPLETED"]),
        artifacts: vec![review_result_artifact()],
        get_task_id: Some("task-from-another-run".into()),
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Halted);
    assert!(
        report.detail.contains("a2a_task_mismatch"),
        "{}",
        report.detail
    );
    let receipts = stage_receipts(&root.run_dir(&report.run_id), "review");
    assert_eq!(receipts.last().unwrap()["operation"], "send_message");
}

#[test]
fn mismatched_context_halts_before_accepting_send_receipt() {
    let server = FakeA2a::start(Script {
        poll_states: states(&["TASK_STATE_COMPLETED"]),
        artifacts: vec![review_result_artifact()],
        context_id: Some("context-from-another-run".into()),
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Halted);
    assert!(report.detail.contains("does not match run context"));
    let receipts = stage_receipts(&root.run_dir(&report.run_id), "review");
    assert_eq!(receipts.len(), 1);
    assert_eq!(receipts[0]["operation"], "card_discovery");
}

#[test]
fn input_required_halts_with_operator_handoff_and_evidence() {
    let server = FakeA2a::start(Script {
        poll_states: states(&["TASK_STATE_SUBMITTED", "TASK_STATE_INPUT_REQUIRED"]),
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Halted);
    assert!(report.detail.contains("TASK_STATE_INPUT_REQUIRED"));
    assert!(report.detail.contains("NOT cancelled"));

    // The failure path keeps its evidence: the drained receipts persist.
    let run_dir = root.run_dir(&report.run_id);
    let receipts = stage_receipts(&run_dir, "review");
    assert_eq!(receipts[0]["operation"], "card_discovery");
    assert_eq!(
        receipts.last().unwrap()["task_state"],
        "TASK_STATE_INPUT_REQUIRED"
    );
}

#[test]
fn timeout_halts_with_task_id_and_no_cancel() {
    let server = FakeA2a::start(Script {
        poll_states: states(&["TASK_STATE_WORKING"]),
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(
        &tmp,
        &server,
        json!({"timeout_seconds": 1, "poll_seconds": 1}),
    );

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Halted);
    assert!(report.detail.contains("task-"));
    assert!(report.detail.contains("NOT cancelled"));

    // The fake agent saw GetTask but never CancelTask — the runner never
    // cancels a product.
    assert!(!server
        .requests()
        .iter()
        .any(|(method, _)| method == "CancelTask"));
}

#[test]
fn failed_task_fails_the_stage() {
    let server = FakeA2a::start(Script {
        poll_states: states(&["TASK_STATE_SUBMITTED", "TASK_STATE_FAILED"]),
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Failed);
    assert!(report.detail.contains("TASK_STATE_FAILED"));
}

#[test]
fn missing_card_fails_the_stage() {
    let server = FakeA2a::start(Script {
        card_missing: true,
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Failed);
    assert!(report.detail.contains("HTTP 404"));
}

#[test]
fn card_pin_mismatch_halts() {
    let server = FakeA2a::start(Script::default());
    let tmp = Tmp::new();
    let wrong_pin = format!("sha256:{}", "f".repeat(64));
    let (root, pipeline_path) = prepare(
        &tmp,
        &server,
        json!({"runtime": {
            "protocol": "a2a",
            "endpoint": server.endpoint,
            "card_url": server.card_url,
            "card_sha256": wrong_pin,
        }}),
    );

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Halted);
    assert!(report.detail.contains("digests to"));
}

#[test]
fn protocol_version_mismatch_halts() {
    let server = FakeA2a::start(Script {
        card: Some(json!({
            "name": "old-agent",
            "description": "pre-1.0 agent",
            "url": "http://127.0.0.1:1/",
            "version": "0.3.0",
            "supportedInterfaces": [{
                "url": "http://127.0.0.1:1/",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "0.3"
            }],
            "capabilities": {},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": []
        })),
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Halted);
    assert!(report.detail.contains("protocolVersion '0.3'"));
}

#[test]
fn direct_message_reply_halts() {
    let server = FakeA2a::start(Script {
        direct_message: true,
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Halted);
    assert!(report.detail.contains("direct message"));

    // The preflight observation survives the halt.
    let run_dir = root.run_dir(&report.run_id);
    let receipts = stage_receipts(&run_dir, "review");
    assert_eq!(receipts[0]["operation"], "card_discovery");
}

#[test]
fn garbage_wire_response_halts() {
    let server = FakeA2a::start(Script {
        garbage_posts: true,
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Halted);
    assert!(report.detail.contains("not JSON"));
}

#[test]
fn artifact_count_mismatch_halts() {
    let server = FakeA2a::start(Script {
        poll_states: states(&["TASK_STATE_COMPLETED"]),
        artifacts: vec![review_result_artifact(), review_result_artifact()],
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Halted);
    assert!(report.detail.contains("contractual"));
}

#[test]
fn artifacts_bind_by_contract_name_not_response_order() {
    let server = FakeA2a::start(Script {
        poll_states: states(&["TASK_STATE_COMPLETED"]),
        artifacts: vec![
            named_artifact("a2", "second.json", "second"),
            named_artifact("a1", "first.json", "first"),
        ],
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) =
        prepare(&tmp, &server, json!({"out": ["first.json", "second.json"]}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Completed);
    let manifest = stammtisch::runner::load_manifest(&root.run_dir(&report.run_id)).unwrap();
    let digests = manifest["stages"][1]["artifacts"].as_array().unwrap();
    let read_marker = |digest: &Value| {
        let hex = digest.as_str().unwrap().strip_prefix("sha256:").unwrap();
        let value: Value = serde_json::from_slice(
            &fs::read(root.run_dir(&report.run_id).join("artifacts").join(hex)).unwrap(),
        )
        .unwrap();
        value["marker"].as_str().unwrap().to_string()
    };
    assert_eq!(read_marker(&digests[0]), "first");
    assert_eq!(read_marker(&digests[1]), "second");
}

#[test]
fn undeclared_artifact_name_halts() {
    let server = FakeA2a::start(Script {
        poll_states: states(&["TASK_STATE_COMPLETED"]),
        artifacts: vec![named_artifact("a1", "wrong.json", "wrong")],
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Halted);
    assert!(report.detail.contains("undeclared artifact name"));
}

#[test]
fn non_json_artifact_halts() {
    let server = FakeA2a::start(Script {
        poll_states: states(&["TASK_STATE_COMPLETED"]),
        artifacts: vec![json!({
            "artifactId": "a1",
            "name": "review.result",
            "parts": [{"text": "just prose, not the contract artifact"}]
        })],
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Halted);
    assert!(report.detail.contains("no JSON part"));

    // Collect failures still salvage the wire evidence.
    let run_dir = root.run_dir(&report.run_id);
    let receipts = stage_receipts(&run_dir, "review");
    assert_eq!(
        receipts.last().unwrap()["task_state"],
        "TASK_STATE_COMPLETED"
    );
}

#[test]
fn agent_error_is_reported_as_product_failure() {
    let server = FakeA2a::start(Script {
        poll_states: states(&["TASK_STATE_COMPLETED"]),
        send_error: Some((-32001, "TaskNotFoundError: no such task".to_string())),
        ..Default::default()
    });
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, &server, json!({}));

    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(report.terminal, Terminal::Failed);
    assert!(report.detail.contains("TaskNotFoundError"));
}

#[test]
fn quinte_without_runtime_is_rejected_at_spec_time() {
    let spec = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": "no-runtime",
        "doctrine": {"pack": "galahad"},
        "stages": [
            {"id": "brief", "product": "doctrine", "out": ["brief.json"]},
            {"id": "review", "product": "quinte", "in": ["brief.json"], "out": ["review.result"]},
        ]
    });
    let err = stammtisch::pipeline::validate(&spec, Path::new("x.json")).unwrap_err();
    assert_eq!(err.code, "quinte_runtime_required");
}

#[test]
fn token_env_declared_but_unset_is_a_hard_error() {
    let spec = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": "token-test",
        "doctrine": {"pack": "galahad"},
        "stages": [
            {"id": "brief", "product": "doctrine", "out": ["brief.json"]},
            {"id": "review", "product": "quinte", "in": ["brief.json"], "out": ["review.result"],
             "runtime": {"protocol": "a2a", "endpoint": "http://127.0.0.1:1/", "token_env": "STAMMTISCH_TEST_UNSET_TOKEN"}},
        ]
    });
    let p = stammtisch::pipeline::validate(&spec, Path::new("x.json")).unwrap();
    // Adapter construction resolves the declared credential: unset = broken
    // config = hard error, never a silent unauthenticated call.
    let err = match stammtisch::adapters::for_stage(&p.stages[1]) {
        Err(e) => e,
        Ok(_) => panic!("adapter construction must fail on a missing declared token"),
    };
    assert_eq!(err.code, "a2a_token_missing");
}
