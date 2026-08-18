//! Live STAMMTISCH → QUINTE call: the shipped A2A adapter drives a real
//! `quinte host serve` endpoint (not FakeA2a, not example.invalid).
//!
//! Gated on `QUINTE_A2A_URL` (optional `QUINTE_A2A_CARD_URL`). The
//! verification harness starts the host and runs this test; default CI
//! stays offline.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use stammtisch::runner::{run_pipeline, Terminal};
use stammtisch::store::StateRoot;

struct Tmp(PathBuf);

impl Tmp {
    fn new() -> Self {
        let dir = std::env::temp_dir().join(format!(
            "stammtisch-quinte-{}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir_all(&dir).unwrap();
        Self(dir)
    }
    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for Tmp {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn live_urls() -> Option<(String, String)> {
    let url = std::env::var("QUINTE_A2A_URL")
        .ok()
        .filter(|s| !s.is_empty())?;
    if url.contains("example.invalid") {
        return None;
    }
    let card = std::env::var("QUINTE_A2A_CARD_URL").unwrap_or_else(|_| {
        let (scheme, rest) = url.split_once("://").unwrap_or(("http", url.as_str()));
        let authority = rest.split('/').next().unwrap_or(rest);
        format!("{scheme}://{authority}/.well-known/agent-card.json")
    });
    Some((url, card))
}

fn prepare(tmp: &Tmp, endpoint: &str, card_url: &str) -> (StateRoot, PathBuf) {
    let root = StateRoot {
        path: tmp.path().join("home"),
    };
    root.init().unwrap();
    let pack_src = Path::new(env!("CARGO_MANIFEST_DIR")).join("doctrine/examples/galahad");
    copy_dir(&pack_src, &tmp.path().join("doctrine/galahad"));

    let spec = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": "quinte-live-call",
        "doctrine": {"pack": "galahad"},
        "stages": [
            {"id": "brief", "product": "doctrine", "out": ["brief.json"],
             "gate": "brief_schema_valid"},
            {
                "id": "review",
                "product": "quinte",
                "in": ["brief.json"],
                "out": ["review.result"],
                "timeout_seconds": 180,
                "poll_seconds": 1,
                "runtime": {
                    "protocol": "a2a",
                    "endpoint": endpoint,
                    "card_url": card_url
                }
            }
        ]
    });
    let pipeline_path = tmp.path().join("pipeline.json");
    fs::write(&pipeline_path, serde_json::to_string_pretty(&spec).unwrap()).unwrap();
    (root, pipeline_path)
}

fn copy_dir(src: &Path, dst: &Path) {
    fs::create_dir_all(dst).unwrap();
    for entry in fs::read_dir(src).unwrap() {
        let entry = entry.unwrap();
        let to = dst.join(entry.file_name());
        if entry.path().is_dir() {
            copy_dir(&entry.path(), &to);
        } else {
            let _ = fs::copy(entry.path(), &to);
        }
    }
}

fn stage_receipts(run_dir: &Path, stage: &str) -> Vec<Value> {
    let mut out = Vec::new();
    let mut n = 0;
    loop {
        let p = run_dir.join("receipts").join(format!("{stage}.{n}.json"));
        if !p.exists() {
            break;
        }
        out.push(serde_json::from_slice(&fs::read(&p).unwrap()).unwrap());
        n += 1;
    }
    out
}

fn collect_result(root: &StateRoot, run_id: &str) -> Value {
    let run_dir = root.run_dir(run_id);
    let manifest: Value =
        serde_json::from_slice(&fs::read(run_dir.join("manifest.json")).unwrap()).unwrap();
    let digest = manifest["stages"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["id"] == "review")
        .and_then(|s| s["artifacts"][0].as_str())
        .expect("review.result digest");
    let hex = digest.strip_prefix("sha256:").unwrap();
    serde_json::from_slice(&fs::read(run_dir.join("artifacts").join(hex)).unwrap()).unwrap()
}

fn one_call(label: &str, endpoint: &str, card_url: &str) -> Value {
    let tmp = Tmp::new();
    let (root, pipeline_path) = prepare(&tmp, endpoint, card_url);
    let report = run_pipeline(&root, &pipeline_path).unwrap();
    assert_eq!(
        report.terminal,
        Terminal::Completed,
        "STAMMTISCH adapter did not complete against {endpoint}: {}",
        report.detail
    );
    let run_dir = root.run_dir(&report.run_id);
    let receipts = stage_receipts(&run_dir, "review");
    assert!(
        !receipts.is_empty(),
        "{label}: review stage left no A2A receipts"
    );
    assert_eq!(
        receipts[0]["operation"], "card_discovery",
        "{label}: first receipt must be card discovery"
    );
    eprintln!(
        "{label} CARD_DISCOVERY agent={} endpoint={} card_sha256={}",
        receipts[0]["host"]["agent"],
        receipts[0]["host"]["endpoint"],
        receipts[0]["host"]["card_sha256"]
    );
    let send = receipts
        .iter()
        .find(|r| r["operation"] == "send_message")
        .expect("send_message receipt");
    let task_id = send["task_id"].as_str().expect("SendMessage task id");
    eprintln!(
        "{label} SENDMESSAGE task_id={task_id} task_state={}",
        send["task_state"]
    );

    let result = collect_result(&root, &report.run_id);
    assert!(result.is_object(), "{result}");
    assert!(result.get("status").is_some(), "missing status: {result}");
    assert!(result.get("run_id").is_some(), "missing run_id: {result}");
    assert!(!result.to_string().to_lowercase().contains("<html"));
    eprintln!(
        "{label} REVIEW.RESULT status={} run_id={}",
        result["status"], result["run_id"]
    );
    result
}

#[test]
fn shipped_adapter_collects_review_result_twice() {
    let Some((endpoint, card_url)) = live_urls() else {
        eprintln!(
            "skipped: set QUINTE_A2A_URL to a live quinte host serve endpoint \
             (not a2a.example.invalid)"
        );
        return;
    };
    eprintln!("LIVE_ENDPOINT {endpoint}");
    let first = one_call("run-1", &endpoint, &card_url);
    let second = one_call("run-2", &endpoint, &card_url);
    assert_eq!(first["status"], second["status"]);
    assert!(first["run_id"].as_str().is_some());
    assert!(second["run_id"].as_str().is_some());
    assert_ne!(
        first["run_id"], second["run_id"],
        "two launches must create two host runs"
    );
    eprintln!(
        "BOTH_RUNS_OK status={} run_id_1={} run_id_2={}",
        first["status"], first["run_id"], second["run_id"]
    );
}
