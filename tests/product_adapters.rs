//! HIGHBALL product adapter: typed, content-addressed upstream carriers plus
//! the digest-pinned QUINTE run product are the only inputs to the real CLI.
//! Ambient workdir files are not evidence.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use stammtisch::adapters::{self, StageContext, Verdict};
use stammtisch::pipeline::{self, Stage};

const REPO: &str = env!("CARGO_MANIFEST_DIR");

fn highball_root() -> PathBuf {
    Path::new(REPO).join("../HIGHBALL")
}

fn highball_available() -> bool {
    highball_root().join("target/debug/highball").is_file()
        || highball_root().join("target/release/highball").is_file()
        || highball_root().join("bin/highball").is_file()
}

fn carrier_values() -> (Value, Value) {
    // Non-executable MEDIUM-risk code change routes to QUINTE, the route
    // that requires the atomic review product the deliver stage attaches.
    let request = json!({
        "question": "Should this code change ship?",
        "action_scope": "Only src/module.rs in this task.",
        "affected_paths": ["src/module.rs"],
        "action_boundary": "reversible",
        "change_class": "code",
        "risk": "MEDIUM",
        "executable": false
    });
    let binding = stammtisch::canon::sha256_value_prefixed(&json!({
        "action_boundary": request["action_boundary"],
        "affected_paths": request["affected_paths"],
        "change_class": request["change_class"],
        "question": request["question"]
    }));
    let trace = json!({
        "trace_version": "1.1",
        "question": request["question"],
        "instrument": "QUINTE",
        "residuals": [],
        "action_boundary": "reversible",
        "highball_decision": "pass",
        "action_binding_sha256": binding
    });
    (request, trace)
}

fn tmp(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "stammtisch-prod-{tag}-{}",
        stammtisch::ids::uuid_v7().unwrap()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn stage_spec(workdir: Option<&Path>) -> Stage {
    let mut deliver = json!({
        "id": "deliver",
        "product": "highball",
        "in": ["highball.route-request.json", "highball.residual-trace.json", "review.result"],
        "out": ["deliver.packet.json"]
    });
    if let Some(path) = workdir {
        deliver["workdir"] = json!(path.to_str().unwrap());
    }
    let v = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": "t-prod",
        "doctrine": {"pack": "galahad"},
        "stages": [
            {"id": "source", "product": "doctrine", "out": [
                "highball.route-request.json", "highball.residual-trace.json", "review.result"
            ]},
            deliver
        ]
    });
    pipeline::validate(&v, Path::new("x.json"))
        .unwrap()
        .stages
        .into_iter()
        .nth(1)
        .unwrap()
}

fn store_input(run_dir: &Path, value: &Value) -> String {
    let bytes = stammtisch::canon::canonical_bytes(value);
    let digest = stammtisch::canon::sha256_prefixed(&bytes);
    std::fs::write(
        run_dir
            .join("artifacts")
            .join(digest.strip_prefix("sha256:").unwrap()),
        bytes,
    )
    .unwrap();
    digest
}

fn empty_pack() -> (PathBuf, stammtisch::doctrine::DoctrinePack) {
    let dir = tmp("pack");
    std::fs::create_dir_all(dir.join("briefs")).unwrap();
    std::fs::write(
        dir.join("doctrine.json"),
        r#"{"pack":"galahad","version":"0.1.0"}"#,
    )
    .unwrap();
    std::fs::write(dir.join("gates.json"), r#"{"gates":[]}"#).unwrap();
    std::fs::write(
        dir.join("briefs").join("brief.template.json"),
        r#"{"schema":"galahad.brief.v0","pipeline":"{{pipeline_id}}","run_id":"{{run_id}}","pack_sha256":"{{pack_sha256}}","objectives":["x"]}"#,
    )
    .unwrap();
    let pack = stammtisch::doctrine::load_dir(&dir).unwrap();
    (dir, pack)
}

/// HIGHBALL digests the brief over its contract field list in field order
/// (jsonutil::canonical_bytes_fields), not sorted-key order.
fn canonical_fields_bytes(value: &Value, fields: &[&str]) -> Vec<u8> {
    let mut out = vec![b'{'];
    for (i, field) in fields.iter().enumerate() {
        if i > 0 {
            out.push(b',');
        }
        out.extend(serde_json::to_string(field).unwrap().as_bytes());
        out.push(b':');
        out.extend(stammtisch::canon::canonical_bytes(
            value.get(*field).unwrap_or(&Value::Null),
        ));
    }
    out.push(b'}');
    out
}

/// A synthetic QUINTE run bound to the route request: the durable run
/// directory (`result.json` / `manifest.json` / `input/brief.json`) plus a
/// pinned fake `quinte` binary whose `inspect` echoes the recorded run
/// state. Returns (state root, pinned binary, review.result summary value).
fn quinte_run_fixture(request: &Value, trace: &Value) -> (PathBuf, PathBuf, Value) {
    let binding = trace["action_binding_sha256"].as_str().unwrap();
    let state = tmp("quinte-state");
    let run_id = stammtisch::ids::uuid_v7().unwrap();
    let run_dir = state.join("runs").join(&run_id);
    std::fs::create_dir_all(run_dir.join("input")).unwrap();
    let seat = json!({
        "seat_id": "seat-test",
        "family": "test-family",
        "provider": "test-provider",
        "text_model": "test-model",
        "multimodal_model": "test-model"
    });
    let parties = [
        "Party A",
        "Party B",
        "Party C",
        "Party D",
        "Party E",
        "Counterpart Arbiter",
        "Primary Arbiter",
    ];
    let route_ids = [
        "route-a", "route-b", "route-c", "route-d", "route-e", "route-f", "route-g",
    ];
    let route_bindings: Vec<Value> = parties
        .iter()
        .zip(route_ids.iter())
        .map(|(party, route)| {
            json!({
                "party_id": party,
                "route_id": route,
                "adapter": "test",
                "executable": "test",
                "family": "test-family",
                "provider": "test-provider",
                "text_model": "test-model",
                "multimodal_model": "test-model",
                "perspective": ""
            })
        })
        .collect();
    let perspectives: Vec<Value> = (0..5)
        .map(|i| {
            json!({
                "party_id": parties[i],
                "route_id": route_ids[i],
                "r1_artifact": format!("lanes/R1/{}/accepted.json", route_ids[i]),
                "r2_artifact": format!("lanes/R2/{}/accepted.json", route_ids[i]),
                "independent_first_pass": true
            })
        })
        .collect();
    let brief = json!({
        "brief_version": "1.1",
        "question": request["question"],
        "context": Value::Null,
        "evidence_roots": [],
        "snapshot_ignore": [],
        "attachments": [],
        "action_scope": request["action_scope"],
        "affected_paths": request["affected_paths"],
        "action_binding_sha256": binding
    });
    let brief_bytes = stammtisch::canon::canonical_bytes(&brief);
    let brief_sha = stammtisch::canon::sha256_prefixed(&canonical_fields_bytes(
        &brief,
        &[
            "brief_version",
            "question",
            "context",
            "evidence_roots",
            "snapshot_ignore",
            "attachments",
            "action_scope",
            "affected_paths",
            "action_binding_sha256",
        ],
    ));
    std::fs::write(run_dir.join("input").join("brief.json"), &brief_bytes).unwrap();
    let result = json!({
        "result_version": "2.1",
        "run_id": run_id,
        "status": "completed",
        "brief_sha256": brief_sha,
        "question": request["question"],
        "action_scope": request["action_scope"],
        "affected_paths": request["affected_paths"],
        "action_binding_sha256": binding,
        "seat_binding": seat.clone(),
        "route_bindings": route_bindings.clone(),
        "summary": "Complete review.",
        "recommendation": "Proceed within scope.",
        "dissent": [],
        "residuals": [],
        "trial_manifest": {
            "manifest_version": "1.0",
            "base_model_relation": "same_model",
            "perspective_count": 5,
            "perspectives": perspectives,
            "perturbation_axes": ["role"],
            "independence_controls": ["independent first pass"],
            "contamination_risks": ["same model"],
            "wall_time_seconds": 1
        }
    });
    let result_bytes = stammtisch::canon::canonical_bytes(&result);
    let result_sha = stammtisch::canon::sha256_prefixed(&result_bytes);
    std::fs::write(run_dir.join("result.json"), &result_bytes).unwrap();
    // The pinned fake `quinte`: `inspect` echoes the recorded run state.
    let bin_dir = tmp("quinte-bin");
    let bin = bin_dir.join("quinte");
    let envelope_path = bin_dir.join("envelope.json");
    std::fs::write(&bin, format!("#!/bin/sh\ncat '{}'\n", envelope_path.display())).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    let runtime_sha = stammtisch::canon::sha256_prefixed(&std::fs::read(&bin).unwrap());
    let manifest = json!({
        "manifest_version": "2.0",
        "run_id": run_id,
        "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": "2026-01-01T00:00:01.000Z",
        "status": "completed",
        "brief_sha256": brief_sha,
        "policy_sha256": format!("sha256:{}", "1".repeat(64)),
        "snapshot_sha256": format!("sha256:{}", "2".repeat(64)),
        "runtime_sha256": runtime_sha,
        "protocol_version": "1.0",
        "effective_model": "test-model",
        "seat_binding": seat,
        "route_bindings": route_bindings,
        "sandbox_mode": "process",
        "current_phase": Value::Null,
        "error": Value::Null,
        "r3_input_receipt": Value::Null,
        "primary_arbiter_challenge": Value::Null,
        "primary_arbiter_submission": Value::Null,
        "result_sha256": result_sha
    });
    std::fs::write(
        run_dir.join("manifest.json"),
        stammtisch::canon::canonical_bytes(&manifest),
    )
    .unwrap();
    let envelope = json!({
        "cli_envelope_version": "1.0",
        "ok": true,
        "data": {"manifest": manifest, "result": result.clone(), "events": []}
    });
    std::fs::write(
        &envelope_path,
        stammtisch::canon::canonical_bytes(&envelope),
    )
    .unwrap();
    (state, bin, result)
}

#[test]
fn highball_requires_both_typed_inputs_at_pipeline_validation() {
    let hb = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": "t-sel",
        "doctrine": {"pack": "galahad"},
        "stages": [{"id": "deliver", "product": "highball"}]
    });
    let err = pipeline::validate(&hb, Path::new("x.json")).unwrap_err();
    assert_eq!(err.code, "highball_input_required", "{err}");
}

#[test]
fn highball_packet_uses_declared_carriers_and_ignores_ambient_workdir() {
    if !highball_available() {
        eprintln!("skip highball_packet_from_staged_inputs: HIGHBALL CLI not in the sibling tree");
        return;
    }
    std::env::set_var("HIGHBALL_HOME", highball_root());

    let ambient = tmp("hb-ambient");
    std::fs::create_dir_all(ambient.join("final")).unwrap();
    std::fs::write(
        ambient.join("route-request.json"),
        r#"{"question":"malicious"}"#,
    )
    .unwrap();
    std::fs::write(
        ambient.join("final/residual-trace.json"),
        r#"{"trace_version":"malicious"}"#,
    )
    .unwrap();
    let (_pack_dir, pack) = empty_pack();
    let run_dir = tmp("hb-run");
    std::fs::create_dir_all(run_dir.join("artifacts")).unwrap();

    let stage = stage_spec(Some(&ambient));
    let (request, trace) = carrier_values();
    let (quinte_state, quinte_bin, review) = quinte_run_fixture(&request, &trace);
    std::env::set_var("QUINTE_HOME", &quinte_state);
    std::env::set_var("HIGHBALL_QUINTE_BIN", &quinte_bin);
    let inputs = std::collections::BTreeMap::from([
        (
            "highball.route-request.json".into(),
            store_input(&run_dir, &request),
        ),
        (
            "highball.residual-trace.json".into(),
            store_input(&run_dir, &trace),
        ),
        ("review.result".into(), store_input(&run_dir, &review)),
    ]);
    let ctx = StageContext {
        run_id: "019b4e5a-2c3d-7e8f-9a0b-1c2d3e4f5a6c",
        pipeline_id: "t-prod",
        stage: &stage,
        doctrine: &pack,
        inputs: &inputs,
        run_dir: &run_dir,
    };
    let adapter = adapters::for_stage(&stage).unwrap();
    adapter.preflight(&ctx).unwrap();
    let handle = adapter.invoke(&ctx).unwrap();
    assert!(matches!(
        adapter.poll(&handle),
        adapters::PollState::Completed
    ));
    let collected = adapter.collect(&handle, &ctx).unwrap();
    assert_eq!(collected.artifacts[0].0, "deliver.packet.json");
    let packet = &collected.artifacts[0].1;
    assert_eq!(packet["packet_version"], "2.0");
    assert_eq!(packet["action_decision"], "pass");
    assert_eq!(packet["route_decision"]["route"], "QUINTE");
    // The adapter attached the QUINTE run product pinned by review.result.
    assert_eq!(packet["product_evidence"]["status"], "complete");
    assert_eq!(packet["route_request"], request);
    assert_ne!(packet["route_request"]["question"], "malicious");
    assert!(matches!(
        stammtisch::adapters::highball::map_action_packet(Some(packet)).unwrap(),
        Verdict::Proceed
    ));
    assert!(matches!(collected.verdict, Verdict::Proceed));
    // The receipt is contract-valid evidence with an authorized decision.
    let (rev, _) = stammtisch::contracts::validate_receipt(
        stammtisch::canon::canonical(&collected.receipts[0]).as_bytes(),
    )
    .unwrap();
    assert_eq!(rev, "highball.action-packet.v1");
    assert_eq!(collected.receipts[0]["decision"], "AUTHORIZED");
}

#[test]
fn highball_input_digest_drift_fails_closed() {
    if !highball_available() {
        eprintln!("skip highball_input_digest_drift_fails_closed: HIGHBALL CLI unavailable");
        return;
    }
    std::env::set_var("HIGHBALL_HOME", highball_root());
    let (_pack_dir, pack) = empty_pack();
    let run_dir = tmp("hb-drift");
    std::fs::create_dir_all(run_dir.join("artifacts")).unwrap();
    let stage = stage_spec(None);
    let (request, trace) = carrier_values();
    let request_digest = store_input(&run_dir, &request);
    let trace_digest = store_input(&run_dir, &trace);
    std::fs::write(
        run_dir
            .join("artifacts")
            .join(request_digest.strip_prefix("sha256:").unwrap()),
        b"{}",
    )
    .unwrap();
    let inputs = std::collections::BTreeMap::from([
        ("highball.route-request.json".into(), request_digest),
        ("highball.residual-trace.json".into(), trace_digest),
    ]);
    let ctx = StageContext {
        run_id: "019b4e5a-2c3d-7e8f-9a0b-1c2d3e4f5a6c",
        pipeline_id: "t-prod",
        stage: &stage,
        doctrine: &pack,
        inputs: &inputs,
        run_dir: &run_dir,
    };
    let adapter = adapters::for_stage(&stage).unwrap();
    let err = adapter.preflight(&ctx).unwrap_err();
    assert_eq!(err.code, "highball_input_digest_drift");
}

#[test]
fn highball_missing_packet_fail_closes() {
    let err = stammtisch::adapters::highball::map_action_packet(None).unwrap_err();
    assert_eq!(err.code, "highball_packet_missing");
}
