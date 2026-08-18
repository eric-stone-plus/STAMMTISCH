//! Three-stage fixture pipeline: doctrine brief → A2A Result 2.1 review →
//! real HIGHBALL Action Packet 2.0. No fakes on the review/deliver path.
//! Drives `stammtisch-core run/export/verify`.

mod support;

use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::{json, Value};

use support::fake_a2a::{FakeA2a, Script};

const BIN: &str = env!("CARGO_BIN_EXE_stammtisch-core");
const REPO: &str = env!("CARGO_MANIFEST_DIR");

fn highball_root() -> PathBuf {
    Path::new(REPO).join("../HIGHBALL")
}

fn highball_available() -> bool {
    highball_root().join("target/debug/highball").is_file()
        || highball_root().join("target/release/highball").is_file()
        || highball_root().join("bin/highball").is_file()
}

fn route_request() -> Value {
    json!({
        "question": "Should this documentation change ship?",
        "action_scope": "docs update",
        "affected_paths": ["docs/readme.md"],
        "action_boundary": "none",
        "change_class": "claim",
        "risk": "LOW",
        "executable": true
    })
}

fn residual_trace() -> Value {
    let request = route_request();
    let binding = stammtisch::canon::sha256_value_prefixed(&json!({
        "action_boundary": request["action_boundary"],
        "affected_paths": request["affected_paths"],
        "change_class": request["change_class"],
        "question": request["question"]
    }));
    json!({
        "trace_version": "1.1",
        "question": request["question"],
        "instrument": "direct-evidence",
        "residuals": [],
        "action_boundary": "none",
        "highball_decision": "pass",
        "action_binding_sha256": binding
    })
}

fn result_21() -> Value {
    json!({
        "result_version": "2.1",
        "run_id": "stammtisch-fixture-review",
        "status": "completed",
        "brief_sha256": format!("sha256:{}", "a".repeat(64)),
        "question": route_request()["question"],
        "action_scope": route_request()["action_scope"],
        "affected_paths": route_request()["affected_paths"],
        "action_binding_sha256": stammtisch::canon::sha256_value_prefixed(&json!({
            "action_boundary": route_request()["action_boundary"],
            "affected_paths": route_request()["affected_paths"],
            "change_class": route_request()["change_class"],
            "question": route_request()["question"]
        })),
        "seat_binding": {
            "seat_id": "seat-g",
            "family": "openai",
            "provider": "openai",
            "text_model": "gpt-5.4",
            "multimodal_model": "gpt-5.4"
        },
        "route_bindings": [
            {"party_id": "Party A", "route_id": "r-a", "adapter": "codex", "executable": "codex", "family": "openai", "provider": "openai", "text_model": "gpt-5.4", "multimodal_model": "gpt-5.4", "perspective": "A"},
            {"party_id": "Party B", "route_id": "r-b", "adapter": "codex", "executable": "codex", "family": "openai", "provider": "openai", "text_model": "gpt-5.4", "multimodal_model": "gpt-5.4", "perspective": "B"},
            {"party_id": "Party C", "route_id": "r-c", "adapter": "codex", "executable": "codex", "family": "openai", "provider": "openai", "text_model": "gpt-5.4", "multimodal_model": "gpt-5.4", "perspective": "C"},
            {"party_id": "Party D", "route_id": "r-d", "adapter": "codex", "executable": "codex", "family": "openai", "provider": "openai", "text_model": "gpt-5.4", "multimodal_model": "gpt-5.4", "perspective": "D"},
            {"party_id": "Party E", "route_id": "r-e", "adapter": "codex", "executable": "codex", "family": "openai", "provider": "openai", "text_model": "gpt-5.4", "multimodal_model": "gpt-5.4", "perspective": "E"},
            {"party_id": "Counterpart Arbiter", "route_id": "r-ca", "adapter": "codex", "executable": "codex", "family": "openai", "provider": "openai", "text_model": "gpt-5.4", "multimodal_model": "gpt-5.4", "perspective": "CA"},
            {"party_id": "Primary Arbiter", "route_id": "r-pa", "adapter": "codex", "executable": "codex", "family": "openai", "provider": "openai", "text_model": "gpt-5.4", "multimodal_model": "gpt-5.4", "perspective": "PA"}
        ],
        "summary": "Review complete.",
        "recommendation": "Ship only after residual closure.",
        "dissent": [],
        "residuals": [],
        "trial_manifest": {
            "manifest_version": "1.0",
            "base_model_relation": "same_model",
            "perspective_count": 5,
            "perspectives": [
                {"party_id": "Party A", "route_id": "r-a", "r1_artifact": "lanes/R1/A.json", "r2_artifact": "lanes/R2/A.json", "independent_first_pass": true},
                {"party_id": "Party B", "route_id": "r-b", "r1_artifact": "lanes/R1/B.json", "r2_artifact": "lanes/R2/B.json", "independent_first_pass": true},
                {"party_id": "Party C", "route_id": "r-c", "r1_artifact": "lanes/R1/C.json", "r2_artifact": "lanes/R2/C.json", "independent_first_pass": true},
                {"party_id": "Party D", "route_id": "r-d", "r1_artifact": "lanes/R1/D.json", "r2_artifact": "lanes/R2/D.json", "independent_first_pass": true},
                {"party_id": "Party E", "route_id": "r-e", "r1_artifact": "lanes/R1/E.json", "r2_artifact": "lanes/R2/E.json", "independent_first_pass": true}
            ],
            "perturbation_axes": ["role"],
            "independence_controls": ["isolated_context"],
            "contamination_risks": ["same_model_error_correlation"],
            "wall_time_seconds": 60
        }
    })
}

fn review_artifact() -> Value {
    json!({
        "artifactId": "a1",
        "name": "review.result",
        "parts": [{
            "data": result_21(),
            "mediaType": "application/json"
        }]
    })
}

fn carrier_artifact(id: &str, name: &str, data: Value) -> Value {
    json!({
        "artifactId": id,
        "name": name,
        "parts": [{"data": data, "mediaType": "application/json"}]
    })
}

struct Out {
    code: i32,
    stdout: String,
    stderr: String,
}

impl Out {
    fn json(&self) -> Value {
        serde_json::from_str(&self.stdout)
            .unwrap_or_else(|e| panic!("stdout is not JSON ({e}):\n{}", self.stdout))
    }
}

fn sh(home: &Path, args: &[&str]) -> Out {
    let out = Command::new(BIN)
        .args(args)
        .env("STAMMTISCH_HOME", home)
        .env("HIGHBALL_HOME", highball_root())
        .output()
        .expect("spawn stammtisch-core");
    Out {
        code: out.status.code().unwrap_or(-1),
        stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
    }
}

fn cp_dir(src: &Path, dst: &Path) {
    std::fs::create_dir_all(dst).unwrap();
    for entry in std::fs::read_dir(src).unwrap() {
        let entry = entry.unwrap();
        let (s, d) = (entry.path(), dst.join(entry.file_name()));
        if s.is_dir() {
            cp_dir(&s, &d);
        } else {
            std::fs::copy(&s, &d).unwrap();
        }
    }
}

fn launch_once(tag: &str, ambient_workdir: &Path, server: &FakeA2a) -> (String, PathBuf, Out, Out) {
    let home = std::env::temp_dir().join(format!(
        "stammtisch-three-{tag}-{}",
        stammtisch::ids::uuid_v7().unwrap()
    ));
    std::fs::create_dir_all(&home).unwrap();
    let pack = home.join("pack");
    cp_dir(&Path::new(REPO).join("doctrine/examples/galahad"), &pack);
    let spec = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": "quant-three-stage",
        "doctrine": {"pack": "galahad", "ref": pack.to_str().unwrap()},
        "stages": [
            {"id": "brief", "product": "doctrine", "out": ["brief.json"],
             "gate": "brief_schema_valid"},
            {
                "id": "review",
                "product": "quinte",
                "in": ["brief.json"],
                "out": [
                    "review.result",
                    "highball.route-request.json",
                    "highball.residual-trace.json"
                ],
                "gate": "quinte_result_21",
                "timeout_seconds": 30,
                "poll_seconds": 0,
                "runtime": {
                    "protocol": "a2a",
                    "endpoint": server.endpoint,
                    "card_url": server.card_url
                }
            },
            {
                "id": "deliver",
                "product": "highball",
                "in": [
                    "review.result",
                    "highball.route-request.json",
                    "highball.residual-trace.json"
                ],
                "out": ["deliver.packet.json"],
                "gate": "packet_authorized",
                "workdir": ambient_workdir.to_str().unwrap()
            }
        ]
    });
    let pipe = home.join("pipeline.json");
    std::fs::write(&pipe, serde_json::to_string_pretty(&spec).unwrap()).unwrap();

    let init = sh(&home, &["init"]);
    assert_eq!(init.code, 0, "init failed: {}", init.stderr);
    let run = sh(
        &home,
        &["run", "--pipeline", pipe.to_str().unwrap(), "--json"],
    );
    assert_eq!(
        run.code, 0,
        "three-stage run failed: {} / {}",
        run.stdout, run.stderr
    );
    let run_id = run.json()["data"]["run_id"].as_str().unwrap().to_string();
    assert_eq!(run.json()["data"]["terminal"], "completed");

    let bundle = home.join("bundle-out");
    let export = sh(
        &home,
        &[
            "export",
            &run_id,
            "--out",
            bundle.to_str().unwrap(),
            "--json",
        ],
    );
    assert_eq!(export.code, 0, "export failed: {}", export.stderr);
    let verify = sh(
        &home,
        &["verify", "--bundle", bundle.to_str().unwrap(), "--json"],
    );
    assert_eq!(
        verify.code, 0,
        "verify --bundle failed: {} / {}",
        verify.stdout, verify.stderr
    );

    // The review stage really went over the wire.
    let wire_log = server.requests();
    let methods: Vec<&str> = wire_log.iter().map(|(m, _)| m.as_str()).collect();
    assert!(methods.contains(&"SendMessage"), "wire log: {methods:?}");
    assert!(methods.contains(&"GetTask"), "wire log: {methods:?}");
    let sent = server.last_message();
    assert_eq!(sent["contextId"], json!(run_id));
    assert!(!sent["parts"].as_array().unwrap().is_empty());

    let review: Value = serde_json::from_slice(
        &std::fs::read(find_artifact(&home, &run_id, "review.result")).unwrap(),
    )
    .unwrap();
    assert_eq!(review["result_version"], "2.1");
    assert!(review.get("run_id").is_some());
    assert!(review.get("residuals").is_some());
    assert!(review.get("recommendation").is_some());

    let packet: Value = serde_json::from_slice(
        &std::fs::read(find_artifact(&home, &run_id, "deliver.packet.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(packet["packet_version"], "2.0");
    assert_eq!(packet["action_decision"], "pass");
    assert_eq!(packet["route_request"], route_request());
    assert_ne!(packet["route_request"]["question"], "ambient poison");

    (run_id, bundle, run, verify)
}

fn find_artifact(home: &Path, run_id: &str, name: &str) -> PathBuf {
    let events =
        std::fs::read_to_string(home.join("runs").join(run_id).join("events.jsonl")).unwrap();
    for line in events.lines() {
        let ev: Value = serde_json::from_str(line).unwrap();
        if ev["type"] == "stage.artifact_recorded" && ev["payload"]["name"] == name {
            let digest = ev["payload"]["digest"].as_str().unwrap();
            let hex = &digest["sha256:".len()..];
            return home.join("runs").join(run_id).join("artifacts").join(hex);
        }
    }
    panic!("artifact {name} not recorded");
}

#[test]
fn three_stage_run_export_verify_twice() {
    if !highball_available() {
        eprintln!("skip three_stage_run_export_verify_twice: HIGHBALL CLI not in the sibling tree");
        return;
    }
    let ambient = std::env::temp_dir().join(format!(
        "stammtisch-three-ambient-{}",
        stammtisch::ids::uuid_v7().unwrap()
    ));
    std::fs::create_dir_all(ambient.join("final")).unwrap();
    std::fs::write(
        ambient.join("route-request.json"),
        r#"{"question":"ambient poison"}"#,
    )
    .unwrap();
    std::fs::write(
        ambient.join("final/residual-trace.json"),
        r#"{"trace_version":"ambient poison"}"#,
    )
    .unwrap();
    let server = FakeA2a::start(Script {
        poll_states: vec!["TASK_STATE_COMPLETED".into()],
        artifacts: vec![
            review_artifact(),
            carrier_artifact("a2", "highball.route-request.json", route_request()),
            carrier_artifact("a3", "highball.residual-trace.json", residual_trace()),
        ],
        ..Default::default()
    });
    let (_id1, _b1, run1, verify1) = launch_once("1", &ambient, &server);
    let (_id2, _b2, run2, verify2) = launch_once("2", &ambient, &server);
    assert_eq!(
        run1.json()["data"]["terminal"],
        run2.json()["data"]["terminal"]
    );
    assert_eq!(verify1.json()["ok"], true);
    assert_eq!(verify2.json()["ok"], true);
}
