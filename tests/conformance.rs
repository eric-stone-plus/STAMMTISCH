//! Conformance and reliability tests — architecture doc §11, items 1–9
//! (plus 10 and the fail-closed gate-kind rule as end-to-end bonuses).
//!
//! Item 6 (gate boundary exactness: == vs >, missing metric, NaN) is
//! covered exhaustively by unit tests in `src/gates.rs`; the integration
//! test below drives the same boundaries through the full CLI pipeline.
//!
//! Every test drives the compiled `stammtisch` binary against a fresh
//! temporary `STAMMTISCH_HOME`; nothing touches the real state root.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::{json, Value};

const BIN: &str = env!("CARGO_BIN_EXE_stammtisch-core");
const REPO: &str = env!("CARGO_MANIFEST_DIR");

struct Tmp(PathBuf);

impl Tmp {
    fn new(tag: &str) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "stammtisch-conf-{tag}-{}",
            stammtisch::ids::uuid_v7().unwrap()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        Tmp(dir)
    }
}

impl Drop for Tmp {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).ok();
    }
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
        .output()
        .expect("spawn stammtisch");
    Out {
        code: out.status.code().unwrap_or(-1),
        stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
    }
}

fn init(home: &Path) {
    let out = sh(home, &["init"]);
    assert_eq!(out.code, 0, "init failed: {}", out.stderr);
}

fn example_pipeline(name: &str) -> String {
    format!("{REPO}/pipelines/examples/{name}")
}

/// Run the happy-path offline slice (fake adapters only) from a copied
/// galahad pack; returns run_id. The shipped security.json example
/// targets a real A2A endpoint by default, so the conformance suite runs
/// this 2-stage offline equivalent instead — everything here stays
/// network-free.
fn run_example(home: &Path) -> String {
    let pack = home.join("pack-offline");
    make_pack(&pack, &|_| {}, &|_| {});
    let pipe = write_pipeline(home, "offline-slice", "galahad", &pack);
    let out = sh(
        home,
        &["run", "--pipeline", pipe.to_str().unwrap(), "--json"],
    );
    assert_eq!(
        out.code, 0,
        "offline run failed: {} / {}",
        out.stdout, out.stderr
    );
    out.json()["data"]["run_id"].as_str().unwrap().to_string()
}

fn export_bundle(home: &Path, run_id: &str, out_dir: &Path) {
    let out = sh(
        home,
        &[
            "export",
            run_id,
            "--out",
            out_dir.to_str().unwrap(),
            "--json",
        ],
    );
    assert_eq!(out.code, 0, "export failed: {}", out.stderr);
}

fn verify(bundle: &Path) -> Out {
    // verify is product- and state-root-independent; still give it a home.
    sh(
        Path::new("/tmp"),
        &["verify", "--bundle", bundle.to_str().unwrap(), "--json"],
    )
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

/// Copy the galahad example pack into `dst` and patch doctrine.json /
/// gates.json with the given mutators.
fn make_pack(dst: &Path, patch_doctrine: &dyn Fn(&mut Value), patch_gates: &dyn Fn(&mut Value)) {
    cp_dir(
        Path::new(REPO).join("doctrine/examples/galahad").as_path(),
        dst,
    );
    let doctrine_path = dst.join("doctrine.json");
    let mut doctrine: Value =
        serde_json::from_str(&std::fs::read_to_string(&doctrine_path).unwrap()).unwrap();
    patch_doctrine(&mut doctrine);
    std::fs::write(
        &doctrine_path,
        serde_json::to_string_pretty(&doctrine).unwrap(),
    )
    .unwrap();
    let gates_path = dst.join("gates.json");
    let mut gates: Value =
        serde_json::from_str(&std::fs::read_to_string(&gates_path).unwrap()).unwrap();
    patch_gates(&mut gates);
    std::fs::write(&gates_path, serde_json::to_string_pretty(&gates).unwrap()).unwrap();
}

/// Pipeline spec pointing at an absolute doctrine pack path.
fn write_pipeline(tmp: &Path, id: &str, pack_name: &str, pack_dir: &Path) -> PathBuf {
    let spec = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": id,
        "doctrine": {"pack": pack_name, "ref": pack_dir.to_str().unwrap()},
        "stages": [
            {"id": "brief", "product": "doctrine", "out": ["brief.json"],
             "gate": "brief_schema_valid"},
            {"id": "deliver", "product": "highball", "adapter": "fake",
             "in": ["brief.json"],
             "out": ["deliver.packet.json"], "gate": "packet_authorized",
             "on_block": "blocked"}
        ]
    });
    let path = tmp.join(format!("{id}.json"));
    std::fs::write(&path, serde_json::to_string_pretty(&spec).unwrap()).unwrap();
    path
}

// --------------------------------------------------------------- item 1

/// §11.1 — schema-valid receipts accepted, schema-invalid rejected; and
/// every artifact of a real run validates against every contract schema.
#[test]
fn item1_contract_validation_every_schema() {
    // Receipt accept/reject at the contract layer.
    let good = json!({
        "schema": "highball.action-packet.v1",
        "packet_id": "pkt-1",
        "route": "direct-evidence",
        "decision": "AUTHORIZED"
    });
    stammtisch::contracts::validate_receipt(serde_json::to_string(&good).unwrap().as_bytes())
        .expect("schema-valid receipt accepted");

    let mut bad = good.clone();
    bad.as_object_mut().unwrap().remove("decision");
    assert!(
        stammtisch::contracts::validate_receipt(serde_json::to_string(&bad).unwrap().as_bytes())
            .is_err(),
        "schema-invalid receipt rejected"
    );

    // End-to-end: every generated document validates against its schema.
    let tmp = Tmp::new("item1");
    init(&tmp.0);
    let run_id = run_example(&tmp.0);
    let run_dir = tmp.0.join("runs").join(&run_id);

    let check = |schema_text: &str, instance: &Value, what: &str| {
        let schema: Value = serde_json::from_str(schema_text).unwrap();
        let errs = stammtisch::jsonval::violations(&schema, instance);
        assert!(errs.is_empty(), "{what} violates schema: {errs:?}");
    };

    // run-event: every line of the authority log
    for line in std::fs::read_to_string(run_dir.join("events.jsonl"))
        .unwrap()
        .lines()
    {
        check(
            stammtisch::schemas::RUN_EVENT,
            &serde_json::from_str(line).unwrap(),
            "event",
        );
    }
    // run-manifest projection
    let manifest: Value =
        serde_json::from_str(&std::fs::read_to_string(run_dir.join("manifest.json")).unwrap())
            .unwrap();
    let manifest_schema = if manifest["schema"] == "stammtisch.manifest.v1" {
        stammtisch::schemas::RUN_MANIFEST_V1
    } else {
        stammtisch::schemas::RUN_MANIFEST
    };
    check(manifest_schema, &manifest, "manifest");
    // gate records
    for entry in std::fs::read_dir(run_dir.join("gates")).unwrap() {
        let record: Value =
            serde_json::from_str(&std::fs::read_to_string(entry.unwrap().path()).unwrap()).unwrap();
        check(stammtisch::schemas::GATE_RECORD, &record, "gate record");
    }
    // bundle manifest
    let bundle_manifest: Value = serde_json::from_str(
        &std::fs::read_to_string(run_dir.join("bundle").join("MANIFEST.json")).unwrap(),
    )
    .unwrap();
    check(
        stammtisch::schemas::BUNDLE_MANIFEST,
        &bundle_manifest,
        "bundle manifest",
    );
    // the shipped example pipeline itself
    let spec: Value =
        serde_json::from_str(&std::fs::read_to_string(example_pipeline("security.json")).unwrap())
            .unwrap();
    check(stammtisch::schemas::PIPELINE, &spec, "pipeline spec");
}

// --------------------------------------------------------------- item 2

/// §11.2 — one active run per state root: a second `run` while the launch
/// lock is held refuses cleanly; `reconcile` clears the stale lock.
#[test]
fn item2_one_active_refuses_second() {
    let tmp = Tmp::new("item2");
    init(&tmp.0);

    // Simulate a crashed/live holder.
    std::fs::write(
        tmp.0.join("host").join("launch.lock"),
        r#"{"run_id":"ghost","pid":1}"#,
    )
    .unwrap();
    let pack = tmp.0.join("pack-offline");
    make_pack(&pack, &|_| {}, &|_| {});
    let pipe = write_pipeline(&tmp.0, "offline-slice", "galahad", &pack);
    let out = sh(
        &tmp.0,
        &["run", "--pipeline", pipe.to_str().unwrap(), "--json"],
    );
    assert_eq!(out.code, 1, "second run must be refused: {:?}", out.code);
    assert!(out.json()["error"]["code"] == "launch_lock_held");
    assert!(out.stderr.contains("reconcile"));

    // Reconcile binds and clears the stale lock; the retry then succeeds.
    let out = sh(&tmp.0, &["reconcile", "--json"]);
    assert_eq!(out.code, 0, "{}", out.stderr);
    assert_eq!(out.json()["data"]["launch_lock"]["removed"], true);
    assert!(!tmp.0.join("host").join("launch.lock").exists());

    let out = sh(
        &tmp.0,
        &["run", "--pipeline", pipe.to_str().unwrap(), "--json"],
    );
    assert_eq!(
        out.code, 0,
        "run after reconcile must succeed: {}",
        out.stderr
    );
}

// --------------------------------------------------------------- item 3

/// §11.3 — corrupt run dir and unknown run id both fail closed.
#[test]
fn item3_corrupt_run_and_unknown_run_fail_closed() {
    let tmp = Tmp::new("item3");
    init(&tmp.0);
    let run_id = run_example(&tmp.0);

    // Unknown run id.
    let out = sh(
        &tmp.0,
        &["status", "019b4e5a-2c3d-7e8f-9a0b-1c2d3e4f5a6b", "--json"],
    );
    assert_eq!(out.code, 3, "unknown run must be a usage error");
    assert_eq!(out.json()["error"]["code"], "run_unknown");

    // Corrupt the authority log (events-first: manifest cannot hide it).
    let events = tmp.0.join("runs").join(&run_id).join("events.jsonl");
    std::fs::OpenOptions::new()
        .append(true)
        .open(&events)
        .map(|mut f| {
            use std::io::Write;
            f.write_all(b"garbage line\n")
        })
        .unwrap()
        .unwrap();
    let out = sh(&tmp.0, &["status", &run_id, "--json"]);
    assert_eq!(
        out.code, 2,
        "corrupt run dir must fail closed: {:?}",
        out.code
    );
    assert_eq!(out.json()["error"]["code"], "run_corrupt");

    // Reconcile reports the corruption instead of guessing — and still
    // completes recovery (lock release, readable runs bound): corrupt dirs
    // are report content, not a reconcile failure.
    let out = sh(&tmp.0, &["reconcile", "--json"]);
    assert_eq!(out.code, 0, "{}", out.stderr);
    let report = out.json();
    assert_eq!(report["data"]["corrupt"].as_array().map(Vec::len), Some(1));
}

#[test]
fn inspect_binds_every_artifact_file_to_events() {
    let tmp = Tmp::new("inspect-artifacts");
    init(&tmp.0);
    let run_id = run_example(&tmp.0);
    let run_dir = tmp.0.join("runs").join(&run_id);

    let baseline = sh(&tmp.0, &["inspect", &run_id, "--json"]);
    assert_eq!(baseline.code, 0, "{}", baseline.stderr);
    assert!(!baseline.json()["data"]["artifacts"]
        .as_array()
        .unwrap()
        .is_empty());

    let artifact = std::fs::read_dir(run_dir.join("artifacts"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    std::fs::remove_file(&artifact).unwrap();
    let missing = sh(&tmp.0, &["inspect", &run_id, "--json"]);
    assert_eq!(missing.code, 2);
    assert_eq!(missing.json()["error"]["code"], "run_corrupt");
}

// --------------------------------------------------------------- item 4

/// §11.4 — crash after stage launch, before receipt: reconcile binds the
/// durable state, marks the run interrupted, and re-invokes nothing.
#[test]
fn item4_crash_before_receipt_reconcile_binds() {
    let tmp = Tmp::new("item4");
    init(&tmp.0);
    let run_id = stammtisch::ids::uuid_v7().unwrap();
    let run_dir = tmp.0.join("runs").join(&run_id);
    std::fs::create_dir_all(&run_dir).unwrap();

    // Simulate a run that died right after stage.started.
    let mut w = stammtisch::store::EventWriter::new(&run_dir, &run_id);
    w.emit(
        "run.created",
        None,
        json!({
            "pipeline": {"id": "p", "canonical_sha256": format!("sha256:{}", "a".repeat(64))},
            "doctrine": {"pack": "galahad", "resolved_sha256": format!("sha256:{}", "b".repeat(64))},
            "stages": [{"id": "brief", "product": "doctrine", "gate": Value::Null, "outputs": ["brief.json"]}],
            "state_root": tmp.0.display().to_string(),
        }),
    )
    .unwrap();
    w.emit("run.staged", None, json!({})).unwrap();
    w.emit(
        "stage.started",
        Some("brief"),
        json!({"product": "doctrine"}),
    )
    .unwrap();

    let out = sh(&tmp.0, &["reconcile", "--json"]);
    assert_eq!(out.code, 0, "{}", out.stderr);
    let data = out.json()["data"].clone();
    let entry = data["runs"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["run_id"] == run_id)
        .expect("reconcile binds the crashed run")
        .clone();
    assert_eq!(entry["interrupted"], true);

    // No work advanced: exactly one audit event was appended, nothing else.
    let events = stammtisch::store::read_events(&run_dir).unwrap();
    assert_eq!(events.len(), 4);
    assert_eq!(events[3]["type"], "run.reconciled");
    assert!(
        events
            .iter()
            .filter(|e| e["type"] == "stage.started")
            .count()
            == 1,
        "no stage relaunch"
    );
    assert!(events.iter().all(|e| e["type"] != "stage.receipt_accepted"));

    // The projection was rebuilt and reflects the durable (interrupted) state.
    let manifest: Value =
        serde_json::from_str(&std::fs::read_to_string(run_dir.join("manifest.json")).unwrap())
            .unwrap();
    assert_eq!(manifest["state"]["code"], "running");
}

// --------------------------------------------------------------- item 5

/// §11.5 — artifact tamper, receipt tamper, gate-log/gate-record tamper:
/// `verify` fails closed every single time.
#[test]
fn item5_tamper_tests_all_detected() {
    let tmp = Tmp::new("item5");
    init(&tmp.0);
    let run_id = run_example(&tmp.0);
    let bundle = tmp.0.join("bundle-orig");
    export_bundle(&tmp.0, &run_id, &bundle);

    // Baseline: untouched bundle verifies.
    let out = verify(&bundle);
    assert_eq!(out.code, 0, "baseline verify: {}", out.stdout);
    assert_eq!(out.json()["data"]["verdict"], "pass");

    // (a) Flip one byte of one artifact.
    let tampered = tmp.0.join("bundle-tamper-artifact");
    cp_dir(&bundle, &tampered);
    let artifact = std::fs::read_dir(tampered.join("artifacts"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let mut bytes = std::fs::read(&artifact).unwrap();
    bytes[10] ^= 0x01;
    std::fs::write(&artifact, bytes).unwrap();
    let out = verify(&tampered);
    assert_eq!(out.code, 2, "artifact tamper must fail");
    assert_eq!(out.json()["data"]["verdict"], "fail");
    assert!(
        out.json()["data"]["failures"]
            .as_array()
            .unwrap()
            .iter()
            .any(|f| f.as_str().unwrap().contains("digest drift")),
        "drift reported: {}",
        out.stdout
    );

    // (b) Alter one receipt field (decision: AUTHORIZED -> DENIED). The
    // flipped value is still schema-valid; digest drift is what convicts.
    let tampered = tmp.0.join("bundle-tamper-receipt");
    cp_dir(&bundle, &tampered);
    let receipt_path = tampered.join("receipts").join("deliver.0.json");
    let mut receipt: Value =
        serde_json::from_str(&std::fs::read_to_string(&receipt_path).unwrap()).unwrap();
    receipt["decision"] = json!("DENIED");
    std::fs::write(&receipt_path, serde_json::to_string(&receipt).unwrap()).unwrap();
    let out = verify(&tampered);
    assert_eq!(out.code, 2, "receipt tamper must fail");
    assert_eq!(out.json()["data"]["verdict"], "fail");

    // (c) Alter the gate record file (decision pass -> fail).
    let tampered = tmp.0.join("bundle-tamper-gaterecord");
    cp_dir(&bundle, &tampered);
    let record_path = tampered.join("gates").join("deliver.gate.json");
    let mut record: Value =
        serde_json::from_str(&std::fs::read_to_string(&record_path).unwrap()).unwrap();
    record["decision"] = json!("fail");
    std::fs::write(&record_path, serde_json::to_string(&record).unwrap()).unwrap();
    let out = verify(&tampered);
    assert_eq!(out.code, 2, "gate-record tamper must fail");
    assert_eq!(out.json()["data"]["verdict"], "fail");

    // (d) Alter the gate *log* inside MANIFEST.json (decision pass -> fail).
    // MANIFEST is not itself digest-pinned inside the bundle; the lie is
    // caught because re-evaluation still computes "pass".
    let tampered = tmp.0.join("bundle-tamper-gatelog");
    cp_dir(&bundle, &tampered);
    let manifest_path = tampered.join("MANIFEST.json");
    let mut manifest: Value =
        serde_json::from_str(&std::fs::read_to_string(&manifest_path).unwrap()).unwrap();
    manifest["gate_log"][1]["decision"] = json!("fail");
    std::fs::write(&manifest_path, serde_json::to_string(&manifest).unwrap()).unwrap();
    let out = verify(&tampered);
    assert_eq!(out.code, 2, "gate-log tamper must fail");
    assert_eq!(out.json()["data"]["verdict"], "fail");
}

/// A run's authoritative completion event pins the exact bundle manifest.
/// Replacing the entire internally self-consistent bundle with another
/// completed run's bundle must therefore be rejected before export.
#[test]
fn completed_run_rejects_whole_bundle_replacement() {
    let tmp = Tmp::new("bundle-binding");
    init(&tmp.0);
    let first = run_example(&tmp.0);
    let second = run_example(&tmp.0);
    let first_bundle = tmp.0.join("runs").join(&first).join("bundle");
    let second_bundle = tmp.0.join("runs").join(&second).join("bundle");

    std::fs::remove_dir_all(&first_bundle).unwrap();
    cp_dir(&second_bundle, &first_bundle);

    let out_dir = tmp.0.join("replaced-bundle-export");
    let out = sh(
        &tmp.0,
        &[
            "export",
            &first,
            "--out",
            out_dir.to_str().unwrap(),
            "--json",
        ],
    );
    assert_eq!(out.code, 2, "whole-bundle replacement must fail closed");
    assert_eq!(out.json()["error"]["code"], "bundle_binding_mismatch");
    assert!(!out_dir.exists(), "no replaced bundle may become visible");
}

#[test]
fn completed_run_rejects_unlisted_bundle_file() {
    let tmp = Tmp::new("bundle-unlisted");
    init(&tmp.0);
    let run_id = run_example(&tmp.0);
    let bundle = tmp.0.join("runs").join(&run_id).join("bundle");
    std::fs::write(bundle.join("unlisted-secret.txt"), b"must not ship").unwrap();

    let out_dir = tmp.0.join("unlisted-bundle-export");
    let out = sh(
        &tmp.0,
        &[
            "export",
            &run_id,
            "--out",
            out_dir.to_str().unwrap(),
            "--json",
        ],
    );
    assert_eq!(out.code, 2, "unlisted bundle content must fail closed");
    assert_eq!(out.json()["error"]["code"], "bundle_binding_invalid");
    assert!(!out_dir.exists(), "no unlisted content may become visible");
}

fn rewrite_manifest_entry(bundle: &Path, rel: &str) {
    let manifest_path = bundle.join("MANIFEST.json");
    let mut manifest: Value =
        serde_json::from_slice(&std::fs::read(&manifest_path).unwrap()).unwrap();
    let bytes = std::fs::read(bundle.join(rel)).unwrap();
    let digest = stammtisch::canon::sha256_prefixed(&bytes);
    let entry = manifest["entries"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|entry| entry["path"] == rel)
        .unwrap();
    entry["sha256"] = json!(digest);
    std::fs::write(
        &manifest_path,
        stammtisch::canon::canonical_bytes(&manifest),
    )
    .unwrap();
}

#[test]
fn verifier_rejects_missing_stage_evidence_even_with_rewritten_manifest() {
    let tmp = Tmp::new("bundle-evidence-closure");
    init(&tmp.0);
    let run_id = run_example(&tmp.0);
    let original = tmp.0.join("bundle-original");
    export_bundle(&tmp.0, &run_id, &original);

    for (tag, kind, stage) in [
        ("artifact", "artifact", "brief"),
        ("receipt", "receipt", "deliver"),
        ("gate", "gate_record", "brief"),
    ] {
        let tampered = tmp.0.join(format!("bundle-missing-{tag}"));
        cp_dir(&original, &tampered);
        let manifest_path = tampered.join("MANIFEST.json");
        let mut manifest: Value =
            serde_json::from_slice(&std::fs::read(&manifest_path).unwrap()).unwrap();
        let index = manifest["entries"]
            .as_array()
            .unwrap()
            .iter()
            .position(|entry| entry["kind"] == kind && entry["stage"] == stage)
            .unwrap();
        let rel = manifest["entries"][index]["path"]
            .as_str()
            .unwrap()
            .to_string();
        manifest["entries"].as_array_mut().unwrap().remove(index);
        if kind == "gate_record" {
            manifest["gate_log"]
                .as_array_mut()
                .unwrap()
                .retain(|entry| entry["stage"] != stage);
        }
        std::fs::remove_file(tampered.join(rel)).unwrap();
        std::fs::write(
            &manifest_path,
            stammtisch::canon::canonical_bytes(&manifest),
        )
        .unwrap();
        let out = verify(&tampered);
        assert_eq!(out.code, 2, "missing {kind} must fail: {}", out.stdout);
    }
}

#[test]
fn verifier_rejects_doctrine_and_receipt_binding_tamper() {
    let tmp = Tmp::new("bundle-semantic-bindings");
    init(&tmp.0);
    let run_id = run_example(&tmp.0);
    let original = tmp.0.join("bundle-original");
    export_bundle(&tmp.0, &run_id, &original);

    let doctrine_tamper = tmp.0.join("bundle-doctrine-pin");
    cp_dir(&original, &doctrine_tamper);
    let manifest_path = doctrine_tamper.join("MANIFEST.json");
    let mut manifest: Value =
        serde_json::from_slice(&std::fs::read(&manifest_path).unwrap()).unwrap();
    manifest["doctrine"]["resolved_sha256"] = json!(format!("sha256:{}", "0".repeat(64)));
    std::fs::write(
        &manifest_path,
        stammtisch::canon::canonical_bytes(&manifest),
    )
    .unwrap();
    let out = verify(&doctrine_tamper);
    assert_eq!(out.code, 2, "false doctrine pin must fail");
    assert!(out.stdout.contains("doctrine pack digests"));

    let highball_tamper = tmp.0.join("bundle-highball-packet-binding");
    cp_dir(&original, &highball_tamper);
    let rel = "receipts/deliver.0.json";
    let receipt_path = highball_tamper.join(rel);
    let mut receipt: Value =
        serde_json::from_slice(&std::fs::read(&receipt_path).unwrap()).unwrap();
    receipt["packet_sha256"] = json!(format!("sha256:{}", "9".repeat(64)));
    std::fs::write(&receipt_path, stammtisch::canon::canonical_bytes(&receipt)).unwrap();
    rewrite_manifest_entry(&highball_tamper, rel);
    let out = verify(&highball_tamper);
    assert_eq!(out.code, 2, "unbound HIGHBALL packet digest must fail");
    assert!(out.stdout.contains("does not bind a same-stage artifact"));
}

#[test]
fn verifier_rejects_zero_evidence_forged_bundle() {
    let tmp = Tmp::new("bundle-zero-evidence");
    let bundle = tmp.0.join("bundle");
    let doctrine = bundle.join("doctrine");
    cp_dir(
        Path::new(REPO).join("doctrine/examples/galahad").as_path(),
        &doctrine,
    );
    let pipeline = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": "forged-zero-evidence",
        "doctrine": {"pack": "galahad"},
        "stages": [{
            "id": "brief", "product": "doctrine", "out": ["brief.json"],
            "gate": "brief_schema_valid"
        }]
    });
    let pipeline_bytes = stammtisch::canon::canonical_bytes(&pipeline);
    std::fs::write(bundle.join("pipeline.json"), &pipeline_bytes).unwrap();
    let mut entries = vec![json!({
        "path": "pipeline.json",
        "sha256": stammtisch::canon::sha256_prefixed(&pipeline_bytes),
        "kind": "spec"
    })];
    for rel in stammtisch::doctrine::pack_files(&doctrine).unwrap() {
        let bytes = std::fs::read(doctrine.join(&rel)).unwrap();
        entries.push(json!({
            "path": format!("doctrine/{rel}"),
            "sha256": stammtisch::canon::sha256_prefixed(&bytes),
            "kind": "doctrine"
        }));
    }
    let manifest = json!({
        "schema": "stammtisch.bundle.v0",
        "run_id": "forged",
        "pipeline": {
            "id": "forged-zero-evidence",
            "canonical_sha256": stammtisch::canon::sha256_value_prefixed(&pipeline)
        },
        "doctrine": {
            "pack": "galahad",
            "resolved_sha256": stammtisch::doctrine::pack_digest(&doctrine).unwrap()
        },
        "entries": entries,
        "gate_log": [],
        "created_at": "2026-08-16T00:00:00.000Z"
    });
    std::fs::write(
        bundle.join("MANIFEST.json"),
        stammtisch::canon::canonical_bytes(&manifest),
    )
    .unwrap();

    let out = verify(&bundle);
    assert_eq!(
        out.code, 2,
        "zero-evidence bundle must fail: {}",
        out.stdout
    );
    assert!(out.stdout.contains("has no receipt evidence"));
    assert!(out.stdout.contains("declares 1 output(s)"));
    assert!(out.stdout.contains("declares gate 'brief_schema_valid'"));
}

// --------------------------------------------------------------- item 6

/// Copied pack plus a `brief_confidence_min` metric gate and a brief
/// template that renders "confidence": 2 — the boundary fixture for §11.6.
fn metric_pack(home: &Path, tag: &str, op: &str) -> PathBuf {
    let pack = home.join(format!("pack-{tag}"));
    make_pack(&pack, &|_| {}, &|gates| {
        gates["gates"].as_array_mut().unwrap().push(json!({
            "id": "brief_confidence_min",
            "kind": "metric_threshold",
            "artifact": "brief.json",
            "metric": "confidence",
            "op": op,
            "value": 2,
            "on_fail": "blocked"
        }));
    });
    let template_path = pack.join("briefs").join("brief.template.json");
    let mut template: Value =
        serde_json::from_str(&std::fs::read_to_string(&template_path).unwrap()).unwrap();
    template["confidence"] = json!(2);
    std::fs::write(
        &template_path,
        serde_json::to_string_pretty(&template).unwrap(),
    )
    .unwrap();
    pack
}

/// Single-stage offline pipeline gating the brief on `brief_confidence_min`.
fn metric_pipeline(tmp: &Path, id: &str, pack_dir: &Path) -> PathBuf {
    let spec = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": id,
        "doctrine": {"pack": "galahad", "ref": pack_dir.to_str().unwrap()},
        "stages": [
            {"id": "brief", "product": "doctrine", "out": ["brief.json"],
             "gate": "brief_confidence_min"}
        ]
    });
    let path = tmp.join(format!("{id}.json"));
    std::fs::write(&path, serde_json::to_string_pretty(&spec).unwrap()).unwrap();
    path
}

/// §11.6 — gate boundary exactness, driven end to end. Unit tests in
/// `src/gates.rs` cover the full matrix (== vs >, missing metric, NaN /
/// non-numeric, unparsable artifact); here the same boundaries must move
/// the *run* to the right terminal state.
#[test]
fn item6_gate_boundaries_end_to_end() {
    // The doctrine fake renders the brief template verbatim, and the
    // patched template carries "confidence": 2.
    // op ">=" with threshold 2 => boundary equality passes.
    let tmp = Tmp::new("item6");
    init(&tmp.0);
    let pack = metric_pack(&tmp.0, "gte", ">=");
    let pipe = metric_pipeline(&tmp.0, "t-boundary-gte", &pack);
    let out = sh(
        &tmp.0,
        &["run", "--pipeline", pipe.to_str().unwrap(), "--json"],
    );
    assert_eq!(out.code, 0, ">= at equality must pass: {}", out.stdout);

    // op ">" with threshold 2 => boundary equality fails => blocked
    // (the gate's on_fail is "blocked").
    let tmp2 = Tmp::new("item6b");
    init(&tmp2.0);
    let pack = metric_pack(&tmp2.0, "gt", ">");
    let pipe = metric_pipeline(&tmp2.0, "t-boundary-gt", &pack);
    let out = sh(
        &tmp2.0,
        &["run", "--pipeline", pipe.to_str().unwrap(), "--json"],
    );
    assert_eq!(out.code, 2, "> at equality must fail: {}", out.stdout);
    let data = out.json()["data"].clone();
    assert_eq!(data["terminal"], "blocked");
    // Durable gate record says fail with the observed value.
    let run_id = data["run_id"].as_str().unwrap();
    let record: Value = serde_json::from_str(
        &std::fs::read_to_string(
            tmp2.0
                .join("runs")
                .join(run_id)
                .join("gates")
                .join("brief.gate.json"),
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(record["decision"], "fail");
    assert_eq!(record["observed"], json!(2));
}

// --------------------------------------------------------------- item 7

/// §11.7 — events-first durability: delete the manifest projection and it
/// is rebuilt from events.jsonl, semantically identical.
#[test]
fn item7_manifest_deleted_rebuilt_from_events() {
    let tmp = Tmp::new("item7");
    init(&tmp.0);
    let run_id = run_example(&tmp.0);
    let manifest_path = tmp.0.join("runs").join(&run_id).join("manifest.json");
    let original: Value =
        serde_json::from_str(&std::fs::read_to_string(&manifest_path).unwrap()).unwrap();

    std::fs::remove_file(&manifest_path).unwrap();
    assert!(!manifest_path.exists());

    let out = sh(&tmp.0, &["status", &run_id, "--json"]);
    assert_eq!(
        out.code, 0,
        "status must rebuild the projection: {}",
        out.stderr
    );
    assert!(manifest_path.exists(), "manifest rewritten on observation");

    let rebuilt: Value =
        serde_json::from_str(&std::fs::read_to_string(&manifest_path).unwrap()).unwrap();
    assert_eq!(
        original, rebuilt,
        "rebuilt projection identical to original"
    );
}

#[test]
fn mixed_run_id_event_log_is_rejected() {
    let tmp = Tmp::new("mixed-run-id");
    init(&tmp.0);
    let run_id = run_example(&tmp.0);
    let events_path = tmp.0.join("runs").join(&run_id).join("events.jsonl");
    let mut lines: Vec<String> = std::fs::read_to_string(&events_path)
        .unwrap()
        .lines()
        .map(str::to_string)
        .collect();
    let mut event: Value = serde_json::from_str(&lines[1]).unwrap();
    event["run_id"] = json!("another-run");
    lines[1] = stammtisch::canon::canonical(&event);
    std::fs::write(&events_path, format!("{}\n", lines.join("\n"))).unwrap();

    let out = sh(&tmp.0, &["status", &run_id, "--json"]);
    assert_eq!(out.code, 2);
    assert_eq!(out.json()["error"]["code"], "run_corrupt");
    assert!(out.json()["error"]["message"]
        .as_str()
        .unwrap()
        .contains("belongs to run"));
}

// --------------------------------------------------------------- item 8

/// §11.8 — blocked pipeline ships nothing: run terminates `blocked`,
/// `export` refuses with exit 2 and creates no output. The blocked path is
/// driven end to end by steering the fake HIGHBALL adapter to a DENIED
/// decision; the deliver stage's `on_block: "blocked"` policy (§5.1) then
/// maps the product refusal to the `blocked` terminal state.
#[test]
fn item8_blocked_pipeline_ships_nothing() {
    let tmp = Tmp::new("item8");
    init(&tmp.0);
    let pack = tmp.0.join("pack-blocked");
    make_pack(
        &pack,
        &|doctrine| {
            doctrine["fixtures"]["highball"] = json!({"decision": "DENIED"});
        },
        &|_| {},
    );
    let pipe = write_pipeline(&tmp.0, "t-blocked", "galahad", &pack);
    let out = sh(
        &tmp.0,
        &["run", "--pipeline", pipe.to_str().unwrap(), "--json"],
    );
    assert_eq!(out.code, 2, "blocked run exits 2: {}", out.stdout);
    let data = out.json()["data"].clone();
    assert_eq!(data["terminal"], "blocked");
    let run_id = data["run_id"].as_str().unwrap().to_string();

    // The evidence explains why.
    let manifest: Value = serde_json::from_str(
        &std::fs::read_to_string(tmp.0.join("runs").join(&run_id).join("manifest.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(manifest["state"]["code"], "blocked");
    assert!(
        !manifest["state"]["blockers"].as_array().unwrap().is_empty(),
        "blockers recorded"
    );

    let out_dir = tmp.0.join("blocked-out");
    let out = sh(
        &tmp.0,
        &[
            "export",
            &run_id,
            "--out",
            out_dir.to_str().unwrap(),
            "--json",
        ],
    );
    assert_eq!(out.code, 2, "export refuses non-completed runs");
    assert_eq!(out.json()["error"]["code"], "export_refused");
    assert!(!out_dir.exists(), "nothing was shipped");
}

// --------------------------------------------------------------- item 9

/// §11.9 — deterministic replay: same bundle bytes => same verdict and a
/// byte-identical report, from any location.
#[test]
fn item9_deterministic_replay() {
    let tmp = Tmp::new("item9");
    init(&tmp.0);
    let run_id = run_example(&tmp.0);
    let bundle = tmp.0.join("bundle");
    export_bundle(&tmp.0, &run_id, &bundle);

    let a = verify(&bundle);
    let b = verify(&bundle);
    assert_eq!(a.code, 0);
    assert_eq!(b.code, 0);
    assert_eq!(a.stdout, b.stdout, "byte-identical report on replay");

    let clone = tmp.0.join("bundle-clone");
    cp_dir(&bundle, &clone);
    let c = verify(&clone);
    assert_eq!(c.code, 0);
    assert_eq!(
        a.stdout, c.stdout,
        "report is a pure function of bundle bytes"
    );
}

// --------------------------------------------------- item 10 (bonus, e2e)

/// §11.10 — adapter contract drift: a product emitting an unknown contract
/// revision halts the run with a durable HALTED record; nothing is parsed
/// best-effort.
#[test]
fn item10_unknown_contract_revision_halts() {
    let tmp = Tmp::new("item10");
    init(&tmp.0);
    let pack = tmp.0.join("pack-drift");
    make_pack(
        &pack,
        &|doctrine| {
            doctrine["fixtures"]["highball"] = json!({"revision": "highball.action-packet.v9"});
        },
        &|_| {},
    );
    let pipe = write_pipeline(&tmp.0, "t-drift", "galahad", &pack);
    let out = sh(
        &tmp.0,
        &["run", "--pipeline", pipe.to_str().unwrap(), "--json"],
    );
    assert_eq!(out.code, 2, "unknown revision must halt: {}", out.stdout);
    let data = out.json()["data"].clone();
    assert_eq!(data["terminal"], "halted");
    assert!(data["detail"]
        .as_str()
        .unwrap()
        .contains("unknown contract revision"));

    // Durable HALTED record in the authority log.
    let run_id = data["run_id"].as_str().unwrap();
    let events = stammtisch::store::read_events(&tmp.0.join("runs").join(run_id)).unwrap();
    let last = events.last().unwrap();
    assert_eq!(last["type"], "run.halted");
    assert_eq!(last["payload"]["reason"], "receipt_rejected");
}

/// Bonus: run deletion — terminal runs are removable; non-terminal runs
/// are refused unless --force; unknown ids are usage errors.
#[test]
fn item_delete_run_cleanup() {
    let tmp = Tmp::new("delete");
    init(&tmp.0);
    let run_id = run_example(&tmp.0); // completed
    let out = sh(&tmp.0, &["delete", &run_id, "--json"]);
    assert_eq!(out.code, 0, "{}", out.stderr);
    assert!(!tmp.0.join("runs").join(&run_id).exists());

    // Unknown run id is a usage error.
    let out = sh(&tmp.0, &["delete", "nope", "--json"]);
    assert_eq!(out.code, 3);
    assert_eq!(out.json()["error"]["code"], "run_unknown");

    // A non-terminal (created) run dir is refused without --force.
    let staged = stammtisch::ids::uuid_v7().unwrap();
    let dir = tmp.0.join("runs").join(&staged);
    std::fs::create_dir_all(&dir).unwrap();
    let mut w = stammtisch::store::EventWriter::new(&dir, &staged);
    w.emit(
        "run.created",
        None,
        json!({
            "pipeline": {"id": "p", "canonical_sha256": format!("sha256:{}", "a".repeat(64))},
            "doctrine": {"pack": "galahad", "resolved_sha256": format!("sha256:{}", "b".repeat(64))},
            "stages": [{"id": "brief", "product": "doctrine", "gate": Value::Null, "outputs": ["brief.json"]}],
            "state_root": tmp.0.display().to_string(),
        }),
    )
    .unwrap();
    let out = sh(&tmp.0, &["delete", &staged, "--json"]);
    assert_eq!(out.code, 3, "non-terminal run must be refused");
    assert_eq!(out.json()["error"]["code"], "run_active");
    assert!(dir.exists(), "refused delete must not remove anything");

    // --force overrides and removes the run.
    let out = sh(&tmp.0, &["delete", &staged, "--force", "--json"]);
    assert_eq!(out.code, 0, "{}", out.stderr);
    assert!(!dir.exists());
}

/// Bonus: unknown gate kind is fail-closed at evaluation (§7).
#[test]
fn unknown_gate_kind_halts() {
    let tmp = Tmp::new("gate-kind");
    init(&tmp.0);
    let pack = tmp.0.join("pack-kind");
    make_pack(&pack, &|_| {}, &|gates| {
        for g in gates["gates"].as_array_mut().unwrap() {
            if g["id"] == "packet_authorized" {
                g["kind"] = json!("llm_vibes");
            }
        }
    });
    let pipe = write_pipeline(&tmp.0, "t-gate-kind", "galahad", &pack);
    let out = sh(
        &tmp.0,
        &["run", "--pipeline", pipe.to_str().unwrap(), "--json"],
    );
    assert_eq!(out.code, 2);
    assert_eq!(out.json()["data"]["terminal"], "halted");
    assert!(out.json()["data"]["detail"]
        .as_str()
        .unwrap()
        .contains("unknown kind"));
}

// --------------------------------------------------------------- P3

/// The per-run cost ledger (roadmap P3) lands in the run directory, ships
/// in the exported bundle with its own manifest entry and digest, and is
/// re-validated offline by `verify`.
#[test]
fn p3_cost_ledger_ships_in_exported_bundle_and_verifies() {
    let tmp = Tmp::new("p3-cost");
    init(&tmp.0);
    let run_id = run_example(&tmp.0);

    // Ledger exists next to the run and validates against its contract.
    let ledger_path = tmp.0.join("runs").join(&run_id).join("cost.json");
    let ledger_bytes = std::fs::read(&ledger_path).expect("run dir has cost.json");
    let ledger: Value = serde_json::from_slice(&ledger_bytes).unwrap();
    assert_eq!(ledger["schema"], "stammtisch.cost-ledger.v0");
    assert_eq!(ledger["run_id"], run_id);
    let schema: Value = serde_json::from_str(stammtisch::schemas::COST_LEDGER).unwrap();
    let errs = stammtisch::jsonval::violations(&schema, &ledger);
    assert!(errs.is_empty(), "cost ledger violates schema: {errs:?}");

    // Both stages are accounted; fake/doctrine receipts carry no token
    // usage, so tokens are null — never invented numbers.
    let stages = ledger["stages"].as_array().unwrap();
    assert_eq!(stages.len(), 2);
    for stage in stages {
        assert_eq!(stage["invocations"], 1);
        assert_eq!(stage["observations"], 1);
        assert_eq!(stage["tokens"]["total"], Value::Null);
        assert!(stage["wall_seconds"].as_f64().is_some());
    }

    // Exported bundle carries cost.json with a kind "cost" manifest entry
    // and a digest that matches the file contents.
    let bundle = tmp.0.join("bundle");
    export_bundle(&tmp.0, &run_id, &bundle);
    let manifest: Value =
        serde_json::from_slice(&std::fs::read(bundle.join("MANIFEST.json")).unwrap()).unwrap();
    let cost_entry = manifest["entries"]
        .as_array()
        .unwrap()
        .iter()
        .find(|entry| entry["path"] == "cost.json")
        .unwrap_or_else(|| panic!("MANIFEST has no cost entry:\n{manifest}"));
    assert_eq!(cost_entry["kind"], "cost");
    assert_eq!(
        cost_entry["sha256"],
        stammtisch::canon::sha256_prefixed(&std::fs::read(bundle.join("cost.json")).unwrap())
    );

    // verify passes, then detects a tampered ledger offline.
    let out = verify(&bundle);
    assert_eq!(out.code, 0, "{}", out.stderr);
    let cost_path = bundle.join("cost.json");
    let mut tampered = std::fs::read(&cost_path).unwrap();
    tampered[0] ^= 0xff;
    std::fs::write(&cost_path, tampered).unwrap();
    let out = verify(&bundle);
    assert_eq!(out.code, 2, "tampered cost ledger must fail verification");
    let report = out.json();
    let failures = report["data"]["failures"].as_array().unwrap();
    assert!(
        failures.iter().any(|f| f
            .as_str()
            .unwrap()
            .contains("digest drift: cost.json")),
        "tamper not reported: {failures:?}"
    );
}

/// `verify --signature FILE` fails closed with a usage error when the
/// signature file does not exist — no minisign needed for this path.
#[test]
fn p3_verify_signature_missing_file_fails_closed() {
    let tmp = Tmp::new("p3-sig");
    init(&tmp.0);
    let run_id = run_example(&tmp.0);
    let bundle = tmp.0.join("bundle");
    export_bundle(&tmp.0, &run_id, &bundle);
    let out = sh(
        &tmp.0,
        &[
            "verify",
            "--bundle",
            bundle.to_str().unwrap(),
            "--signature",
            "/nonexistent/minisig.file",
            "--json",
        ],
    );
    assert_eq!(out.code, 3, "missing signature file must be a usage error");
    assert_eq!(out.json()["error"]["code"], "signature_missing");
}
