//! HIGHBALL product adapter: typed, content-addressed upstream carriers are
//! the only inputs to the real CLI. Ambient workdir files are not evidence.

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
    let request = json!({
        "question": "Should this documentation change ship?",
        "action_scope": "docs update",
        "affected_paths": ["docs/readme.md"],
        "action_boundary": "none",
        "change_class": "claim",
        "risk": "LOW",
        "executable": true
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
        "instrument": "direct-evidence",
        "residuals": [],
        "action_boundary": "none",
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
        "in": ["highball.route-request.json", "highball.residual-trace.json"],
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
                "highball.route-request.json", "highball.residual-trace.json"
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
    let inputs = std::collections::BTreeMap::from([
        (
            "highball.route-request.json".into(),
            store_input(&run_dir, &request),
        ),
        (
            "highball.residual-trace.json".into(),
            store_input(&run_dir, &trace),
        ),
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
    assert_eq!(packet["route_request"], request);
    assert_ne!(packet["route_request"]["question"], "malicious");
    assert!(matches!(
        stammtisch::adapters::highball::map_action_packet(Some(packet)).unwrap(),
        Verdict::Proceed
    ));
    assert!(matches!(collected.verdict, Verdict::Proceed));
    // The receipt is contract-valid evidence with a direct-evidence route.
    let (rev, _) = stammtisch::contracts::validate_receipt(
        stammtisch::canon::canonical(&collected.receipts[0]).as_bytes(),
    )
    .unwrap();
    assert_eq!(rev, "highball.action-packet.v1");
    assert_eq!(collected.receipts[0]["route"], "direct-evidence");
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
