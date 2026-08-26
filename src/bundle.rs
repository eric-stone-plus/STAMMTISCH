//! Deliverable bundle (architecture doc §6): assembled at run completion,
//! exported with `export`, and re-verified offline with `verify`.
//!
//! `verify` is a pure function of bundle bytes: it re-checks every digest
//! in MANIFEST.json, re-validates every receipt against its pinned contract
//! revision, re-validates gate records against their schema, and
//! re-evaluates every gate from the bundled artifacts and doctrine — then
//! compares the recomputed decision and observed value against the record
//! and the gate log. No product installation involved.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path};

use serde_json::{json, Value};

use crate::canon;
use crate::doctrine::DoctrinePack;
use crate::error::AppError;
use crate::gates;
use crate::pipeline::Pipeline;
use crate::store::{atomic_write, StateRoot};

/// Assemble `runs/<id>/bundle/` at run completion. Returns the SHA-256 of
/// MANIFEST.json — recorded in the run.completed event payload.
pub fn assemble(
    run_dir: &Path,
    run_id: &str,
    pipeline: &Pipeline,
    doctrine: &DoctrinePack,
) -> Result<String, AppError> {
    let bundle = run_dir.join("bundle");
    for d in ["artifacts", "receipts", "gates", "doctrine"] {
        std::fs::create_dir_all(bundle.join(d))?;
    }

    let mut entries: Vec<Value> = Vec::new();

    // Canonical spec — provenance root.
    let spec_bytes = std::fs::read(run_dir.join("pipeline.json"))?;
    let spec_digest = canon::sha256_prefixed(&spec_bytes);
    atomic_write(&bundle.join("pipeline.json"), &spec_bytes)?;
    entries.push(json!({
        "path": "pipeline.json", "sha256": spec_digest, "kind": "spec",
    }));

    // Whole doctrine pack (small, declarative) — needed offline for gate
    // re-evaluation and schema_check.
    for rel in crate::doctrine::pack_files(&doctrine.dir)? {
        let bytes = std::fs::read(doctrine.dir.join(&rel))?;
        let digest = canon::sha256_prefixed(&bytes);
        let dest = bundle.join("doctrine").join(&rel);
        atomic_write(&dest, &bytes)?;
        entries.push(json!({
            "path": format!("doctrine/{rel}"), "sha256": digest, "kind": "doctrine",
        }));
    }

    // Per-stage evidence in pipeline order: artifacts first, then the
    // receipts that reference them, then the gate record.
    let events = crate::store::read_events(run_dir)?;
    let mut gate_log: Vec<Value> = Vec::new();
    for stage in &pipeline.stages {
        for event in &events {
            if event.get("stage").and_then(Value::as_str) != Some(stage.id.as_str()) {
                continue;
            }
            match event["type"].as_str().expect("schema-checked") {
                "stage.artifact_recorded" => {
                    let digest = event["payload"]["digest"].as_str().expect("schema-checked");
                    let hex = &digest["sha256:".len()..];
                    let bytes = std::fs::read(run_dir.join("artifacts").join(hex))?;
                    atomic_write(&bundle.join("artifacts").join(hex), &bytes)?;
                    entries.push(json!({
                        "path": format!("artifacts/{hex}"), "sha256": digest,
                        "kind": "artifact", "stage": stage.id,
                    }));
                }
                "stage.receipt_accepted" => {
                    let digest = event["payload"]["digest"].as_str().expect("schema-checked");
                    // Locate the receipt file by digest (names carry the index).
                    let rel = find_receipt_path(run_dir, &stage.id, digest)?;
                    let bytes = std::fs::read(run_dir.join(&rel))?;
                    atomic_write(&bundle.join(&rel), &bytes)?;
                    entries.push(json!({
                        "path": rel, "sha256": digest,
                        "kind": "receipt", "stage": stage.id,
                    }));
                }
                "stage.gate_passed" | "stage.gate_failed" => {
                    let payload = &event["payload"];
                    let record_digest = payload["record_sha256"].as_str().expect("schema-checked");
                    let rel = format!("gates/{}.gate.json", stage.id);
                    let bytes = std::fs::read(run_dir.join(&rel))?;
                    if canon::sha256_prefixed(&bytes) != record_digest {
                        return Err(AppError::integrity(
                            "gate_record_drift",
                            format!("{rel} does not match its recorded digest"),
                        ));
                    }
                    atomic_write(&bundle.join(&rel), &bytes)?;
                    entries.push(json!({
                        "path": rel, "sha256": record_digest,
                        "kind": "gate_record", "stage": stage.id,
                    }));
                    gate_log.push(json!({
                        "gate_id": payload["gate_id"],
                        "stage": stage.id,
                        "decision": payload["decision"],
                        "observed": payload.get("observed").cloned().unwrap_or(Value::Null),
                        "record_sha256": record_digest,
                    }));
                }
                _ => {}
            }
        }
    }

    // Per-run cost ledger (roadmap P3): shipped when present. Cost
    // accounting is fail-safe — a run whose ledger could not be written
    // still completes and exports without one.
    let cost_path = run_dir.join("cost.json");
    if cost_path.is_file() {
        let bytes = std::fs::read(&cost_path)?;
        let digest = canon::sha256_prefixed(&bytes);
        atomic_write(&bundle.join("cost.json"), &bytes)?;
        entries.push(json!({
            "path": "cost.json", "sha256": digest, "kind": "cost",
        }));
    }

    let manifest = json!({
        "schema": "stammtisch.bundle.v0",
        "run_id": run_id,
        "pipeline": {"id": pipeline.id, "canonical_sha256": pipeline.canonical_sha256},
        "doctrine": {"pack": doctrine.name, "resolved_sha256": doctrine.digest},
        "entries": entries,
        "gate_log": gate_log,
        "created_at": crate::time::now_rfc3339(),
    });
    let schema: Value =
        serde_json::from_str(crate::schemas::BUNDLE_MANIFEST).expect("embedded schema parses");
    let errs = crate::jsonval::violations(&schema, &manifest);
    if !errs.is_empty() {
        return Err(AppError::internal(format!(
            "bundle manifest violates schema: {}",
            errs.join("; ")
        )));
    }
    let bytes = canon::canonical_bytes(&manifest);
    atomic_write(&bundle.join("MANIFEST.json"), &bytes)?;
    let (pass, report) = verify(&bundle)?;
    if !pass {
        let failures = report["failures"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join("; ")
            })
            .unwrap_or_else(|| "bundle verification failed".to_string());
        return Err(AppError::integrity(
            "bundle_assembly_invalid",
            format!("assembled bundle fails offline verification: {failures}"),
        ));
    }
    Ok(canon::sha256_prefixed(&bytes))
}

fn find_receipt_path(run_dir: &Path, stage: &str, digest: &str) -> Result<String, AppError> {
    // Scan the receipts directory instead of probing a fixed index range:
    // a stage's receipt count is not bounded by any constant — a real A2A
    // review stage routinely emits 17+ (preflight/start/poll/collect)
    // receipts, and a fixed 0..16 probe silently misses the tail.
    let prefix = format!("{stage}.");
    let dir = run_dir.join("receipts");
    let mut names: Vec<String> = Vec::new();
    for entry in std::fs::read_dir(&dir).map_err(|e| {
        AppError::integrity(
            "receipt_missing",
            format!("cannot list receipts directory {}: {e}", dir.display()),
        )
    })? {
        let entry = entry.map_err(|e| {
            AppError::integrity("receipt_missing", format!("cannot read receipt entry: {e}"))
        })?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with(&prefix) && name.ends_with(".json") {
            names.push(name);
        }
    }
    names.sort();
    for name in names {
        let rel = format!("receipts/{name}");
        let path = run_dir.join(&rel);
        if canon::sha256_prefixed(&std::fs::read(&path)?) == digest {
            return Ok(rel);
        }
    }
    Err(AppError::integrity(
        "receipt_missing",
        format!("no receipt file for stage '{stage}' matching {digest}"),
    ))
}

/// `stammtisch export RUN_ID --out DIR`. Blocked pipelines ship nothing:
/// only `completed` runs export.
pub fn export(root: &StateRoot, run_id: &str, out: &Path) -> Result<Value, AppError> {
    let run_dir = root.run_dir(run_id);
    if !run_dir.is_dir() {
        return Err(AppError::usage(
            "run_unknown",
            format!("no run with id '{run_id}' in this state root"),
        ));
    }
    let manifest = crate::runner::load_manifest(&run_dir)?;
    let state = manifest["state"]["code"].as_str().unwrap_or("unknown");
    if state != "completed" {
        return Err(AppError::integrity(
            "export_refused",
            format!("run '{run_id}' is '{state}', not completed — non-completed runs ship nothing"),
        ));
    }
    let bundle = run_dir.join("bundle");
    if !bundle.is_dir() {
        return Err(AppError::integrity(
            "run_corrupt",
            format!("run '{run_id}' completed but has no bundle/ directory"),
        ));
    }
    let pinned = manifest
        .get("bundle_manifest_sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            AppError::integrity(
                "run_corrupt",
                format!("run '{run_id}' completed without an event-pinned bundle manifest digest"),
            )
        })?;
    validate_run_bundle_binding(&bundle, run_id, pinned)?;
    if out.exists() {
        return Err(AppError::usage(
            "export_target_exists",
            format!(
                "export target {} already exists; refusing to overwrite",
                out.display()
            ),
        ));
    }
    let tmp = out.with_extension(format!("tmp-{}", std::process::id()));
    if tmp.exists() {
        return Err(AppError::usage(
            "export_target_exists",
            format!("temporary export target {} already exists", tmp.display()),
        ));
    }
    if let Err(e) = copy_dir(&bundle, &tmp).and_then(|_| {
        // Re-check the copied bytes before making them visible. This catches
        // source mutation during export as well as a replaced run bundle.
        validate_run_bundle_binding(&tmp, run_id, pinned)?;
        std::fs::rename(&tmp, out)?;
        Ok(())
    }) {
        let _ = std::fs::remove_dir_all(&tmp);
        return Err(e);
    }
    let manifest_bytes = std::fs::read(out.join("MANIFEST.json"))?;
    Ok(json!({
        "run_id": run_id,
        "out": out.display().to_string(),
        "manifest_sha256": canon::sha256_prefixed(&manifest_bytes),
    }))
}

fn validate_run_bundle_binding(bundle: &Path, run_id: &str, pinned: &str) -> Result<(), AppError> {
    let manifest_path = bundle.join("MANIFEST.json");
    let bytes = std::fs::read(&manifest_path).map_err(|e| {
        AppError::integrity(
            "bundle_binding_invalid",
            format!("{} is unreadable: {e}", manifest_path.display()),
        )
    })?;
    let actual = canon::sha256_prefixed(&bytes);
    if actual != pinned {
        return Err(AppError::integrity(
            "bundle_binding_mismatch",
            format!("bundle/MANIFEST.json hashes to {actual}, but run.completed pins {pinned}"),
        ));
    }
    let bundle_manifest: Value = serde_json::from_slice(&bytes).map_err(|e| {
        AppError::integrity(
            "bundle_binding_invalid",
            format!("bundle/MANIFEST.json is unparseable: {e}"),
        )
    })?;
    let schema: Value =
        serde_json::from_str(crate::schemas::BUNDLE_MANIFEST).expect("embedded schema parses");
    let errors = crate::jsonval::violations(&schema, &bundle_manifest);
    if !errors.is_empty() {
        return Err(AppError::integrity(
            "bundle_binding_invalid",
            format!(
                "bundle/MANIFEST.json violates schema: {}",
                errors.join("; ")
            ),
        ));
    }
    if bundle_manifest.get("run_id").and_then(Value::as_str) != Some(run_id) {
        return Err(AppError::integrity(
            "bundle_binding_mismatch",
            format!("bundle manifest is not bound to run '{run_id}'"),
        ));
    }
    let (pass, report) = verify(bundle)?;
    if !pass {
        let failures = report["failures"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join("; ")
            })
            .unwrap_or_else(|| "bundle verification failed".to_string());
        return Err(AppError::integrity(
            "bundle_binding_invalid",
            format!("event-pinned run bundle fails offline verification: {failures}"),
        ));
    }
    Ok(())
}

fn copy_dir(src: &Path, dst: &Path) -> Result<(), AppError> {
    std::fs::create_dir_all(dst)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let (s, d) = (entry.path(), dst.join(entry.file_name()));
        if s.is_dir() {
            copy_dir(&s, &d)?;
        } else {
            std::fs::copy(&s, &d)?;
        }
    }
    Ok(())
}

// ------------------------------------------------------------------ verify

/// Offline verification verdict + deterministic report (no timestamps, no
/// local paths — same bundle bytes, byte-identical report).
pub fn verify(bundle: &Path) -> Result<(bool, Value), AppError> {
    let mut failures: Vec<String> = Vec::new();

    let manifest_path = bundle.join("MANIFEST.json");
    let manifest_bytes = std::fs::read(&manifest_path).map_err(|e| {
        AppError::integrity(
            "bundle_invalid",
            format!("{}: {e}", manifest_path.display()),
        )
    })?;
    let manifest: Value = serde_json::from_slice(&manifest_bytes).map_err(|e| {
        AppError::integrity("bundle_invalid", format!("MANIFEST.json unparseable: {e}"))
    })?;
    let bundle_schema: Value =
        serde_json::from_str(crate::schemas::BUNDLE_MANIFEST).expect("embedded schema parses");
    for err in crate::jsonval::violations(&bundle_schema, &manifest) {
        failures.push(format!("MANIFEST.json violates bundle schema: {err}"));
    }

    let entries = manifest
        .get("entries")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();

    // 1. Re-check every digest.
    let mut receipt_count = 0usize;
    let mut gate_record_count = 0usize;
    let mut listed_paths = BTreeSet::new();
    for entry in &entries {
        let rel = entry.get("path").and_then(Value::as_str).unwrap_or("");
        if !safe_bundle_path(rel) {
            failures.push(format!("entry has unsafe path '{rel}'"));
            continue;
        }
        if !listed_paths.insert(rel.to_string()) {
            failures.push(format!("entry path '{rel}' appears more than once"));
        }
        let want = entry.get("sha256").and_then(Value::as_str).unwrap_or("");
        let bytes = std::fs::read(bundle.join(rel));
        match &bytes {
            Ok(bytes) => {
                let got = canon::sha256_prefixed(bytes);
                if got != want {
                    failures.push(format!(
                        "digest drift: {rel} hashes to {got}, manifest says {want}"
                    ));
                }
            }
            Err(e) => failures.push(format!("entry '{rel}' unreadable: {e}")),
        }
        match entry.get("kind").and_then(Value::as_str) {
            Some("receipt") => receipt_count += 1,
            Some("gate_record") => gate_record_count += 1,
            Some("cost") => {
                // The cost ledger is re-validated against its own contract
                // like every other bundled artifact (roadmap P3).
                if let Ok(ledger_bytes) = &bytes {
                    let cost_schema: Value = serde_json::from_str(crate::schemas::COST_LEDGER)
                        .expect("embedded schema parses");
                    match serde_json::from_slice::<Value>(ledger_bytes) {
                        Ok(ledger) => {
                            for err in crate::jsonval::violations(&cost_schema, &ledger) {
                                failures.push(format!(
                                    "cost ledger '{rel}' violates schema: {err}"
                                ));
                            }
                        }
                        Err(e) => failures.push(format!("cost ledger '{rel}' unparseable: {e}")),
                    }
                }
            }
            _ => {}
        }
    }
    match bundle_files(bundle) {
        Ok(actual_paths) => {
            let expected_paths: BTreeSet<String> = listed_paths
                .iter()
                .cloned()
                .chain(std::iter::once("MANIFEST.json".to_string()))
                .collect();
            for unlisted in actual_paths.difference(&expected_paths) {
                failures.push(format!("unlisted bundle file '{unlisted}'"));
            }
            for missing in expected_paths.difference(&actual_paths) {
                failures.push(format!("listed bundle file '{missing}' is absent"));
            }
        }
        Err(e) => failures.push(format!("cannot enumerate bundle files: {e}")),
    }

    // 2. Pipeline spec provenance: canonical digest of bundled spec must
    //    equal the manifest's pinned digest.
    let mut pipeline_spec = None;
    if let Ok(spec_bytes) = std::fs::read(bundle.join("pipeline.json")) {
        match serde_json::from_slice::<Value>(&spec_bytes) {
            Ok(spec) => {
                let canonical = canon::sha256_value_prefixed(&spec);
                let pinned = manifest
                    .pointer("/pipeline/canonical_sha256")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                if canonical != pinned {
                    failures.push(format!(
                        "pipeline spec digests to {canonical}, manifest pins {pinned}"
                    ));
                }
                let pipeline_schema: Value =
                    serde_json::from_str(crate::schemas::PIPELINE).expect("embedded schema parses");
                let schema_errors = crate::jsonval::violations(&pipeline_schema, &spec);
                for error in schema_errors {
                    failures.push(format!("pipeline.json violates pipeline schema: {error}"));
                }
                if spec.pointer("/id") != manifest.pointer("/pipeline/id") {
                    failures.push("pipeline id does not match bundle manifest".to_string());
                }
                if spec.pointer("/doctrine/pack") != manifest.pointer("/doctrine/pack") {
                    failures
                        .push("pipeline doctrine pack does not match bundle manifest".to_string());
                }
                pipeline_spec = Some(spec);
            }
            Err(e) => failures.push(format!("pipeline.json unparseable: {e}")),
        }
    } else {
        failures.push("pipeline.json missing".to_string());
    }

    // The doctrine provenance pin covers the complete bundled pack, not
    // merely the individual entry digests.  Recompute the same sorted
    // relpath/file-digest listing used at run time.
    match crate::doctrine::pack_digest(&bundle.join("doctrine")) {
        Ok(actual) => {
            let pinned = manifest
                .pointer("/doctrine/resolved_sha256")
                .and_then(Value::as_str)
                .unwrap_or("");
            if actual != pinned {
                failures.push(format!(
                    "doctrine pack digests to {actual}, manifest pins {pinned}"
                ));
            }
        }
        Err(e) => failures.push(format!("doctrine pack cannot be digested: {e}")),
    }

    if let Some(spec) = &pipeline_spec {
        validate_evidence_closure(&entries, &manifest, spec, &mut failures);
    }

    // 3. Re-validate every receipt against its pinned contract revision.
    for entry in &entries {
        if entry.get("kind").and_then(Value::as_str) != Some("receipt") {
            continue;
        }
        // The schema pass above only accumulates failures; it does not
        // stop this walk, so a malformed manifest still reaches here.
        let Some(rel) = entry.get("path").and_then(Value::as_str) else {
            failures.push("receipt entry 'path' is not a string".to_string());
            continue;
        };
        match std::fs::read(bundle.join(rel)) {
            Ok(bytes) => match crate::contracts::validate_receipt(&bytes) {
                Err(e) => failures.push(format!("receipt '{rel}' rejected: {e}")),
                Ok((revision, receipt)) => {
                    validate_receipt_binding(
                        &revision,
                        &receipt,
                        entry,
                        &entries,
                        &manifest,
                        &mut failures,
                    );
                }
            },
            Err(e) => failures.push(format!("receipt '{rel}' unreadable: {e}")),
        }
    }

    // 4. Gate records: schema, digest vs gate log, decision consistency,
    //    then full re-evaluation from bundled artifacts + doctrine.
    let gate_log = manifest
        .get("gate_log")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let gate_schema: Value =
        serde_json::from_str(crate::schemas::GATE_RECORD).expect("embedded schema parses");
    let gates_def_path = bundle.join("doctrine").join("gates.json");
    let gate_defs_doc: Option<Value> = std::fs::read_to_string(&gates_def_path)
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok());
    if gate_defs_doc.is_none() {
        failures.push("doctrine/gates.json unreadable; gates cannot be re-evaluated".to_string());
    }

    for entry in &entries {
        if entry.get("kind").and_then(Value::as_str) != Some("gate_record") {
            continue;
        }
        let Some(rel) = entry.get("path").and_then(Value::as_str) else {
            failures.push("gate record entry 'path' is not a string".to_string());
            continue;
        };
        let stage = entry.get("stage").and_then(Value::as_str).unwrap_or("");
        let bytes = match std::fs::read(bundle.join(rel)) {
            Ok(b) => b,
            Err(e) => {
                failures.push(format!("gate record '{rel}' unreadable: {e}"));
                continue;
            }
        };
        let record: Value = match serde_json::from_slice(&bytes) {
            Ok(v) => v,
            Err(e) => {
                failures.push(format!("gate record '{rel}' unparseable: {e}"));
                continue;
            }
        };
        for err in crate::jsonval::violations(&gate_schema, &record) {
            failures.push(format!("gate record '{rel}' violates schema: {err}"));
        }
        let record_digest = canon::sha256_prefixed(&bytes);
        let log_entry = gate_log.iter().find(|g| {
            g.get("stage").and_then(Value::as_str) == Some(stage)
                && g.get("gate_id") == record.get("gate_id")
        });
        match log_entry {
            Some(g) => {
                if g.get("record_sha256").and_then(Value::as_str) != Some(record_digest.as_str()) {
                    failures.push(format!(
                        "gate log record digest mismatch for '{rel}' (log pins {:?})",
                        g.get("record_sha256")
                    ));
                }
                if g.get("decision") != record.get("decision") {
                    failures.push(format!(
                        "gate log decision {:?} != record decision {:?} for '{rel}'",
                        g.get("decision"),
                        record.get("decision")
                    ));
                }
                if g.get("observed") != record.get("observed") {
                    failures.push(format!(
                        "gate log observed {:?} != record observed {:?} for '{rel}'",
                        g.get("observed"),
                        record.get("observed")
                    ));
                }
            }
            None => failures.push(format!("gate record '{rel}' has no gate_log entry")),
        }
        if record.get("run_id") != manifest.get("run_id") {
            failures.push(format!("gate record '{rel}' run_id mismatch"));
        }

        // Re-evaluation.
        if let Some(doc) = &gate_defs_doc {
            let gate_id = record.get("gate_id").and_then(Value::as_str).unwrap_or("");
            let raw_def = doc
                .get("gates")
                .and_then(Value::as_array)
                .and_then(|gs| {
                    gs.iter()
                        .find(|g| g.get("id").and_then(Value::as_str) == Some(gate_id))
                })
                .cloned();
            match raw_def {
                None => failures.push(format!(
                    "gate '{gate_id}' not defined in bundled doctrine/gates.json"
                )),
                Some(raw) => match gates::parse_def(&raw) {
                    Err(e) => failures.push(format!("gate '{gate_id}' def invalid: {e}")),
                    Ok(def) => {
                        reevaluate_gate(bundle, &entries, &def, &record, stage, &mut failures);
                    }
                },
            }
        }
    }

    let gates_report: Vec<Value> = gate_log
        .iter()
        .map(|g| {
            json!({
                "gate_id": g.get("gate_id").cloned().unwrap_or(Value::Null),
                "stage": g.get("stage").cloned().unwrap_or(Value::Null),
                "decision": g.get("decision").cloned().unwrap_or(Value::Null),
                "observed": g.get("observed").cloned().unwrap_or(Value::Null),
            })
        })
        .collect();

    let pass = failures.is_empty();
    let report = json!({
        "verdict": if pass { "pass" } else { "fail" },
        "failures": failures,
        "entries": entries.len(),
        "receipts": receipt_count,
        "gate_records": gate_record_count,
        "gates": gates_report,
    });
    Ok((pass, report))
}

/// Optional detached-signature verification hook (roadmap P3): `verify
/// --bundle DIR --signature FILE` checks a minisign detached signature over
/// the bundle's MANIFEST.json.
///
/// Fail-closed by design: when a signature is requested, an absent
/// signature file, an absent minisign binary, or a rejected signature is an
/// error — never a silent skip. Signing stays an operator action; the core
/// never invokes minisign during export.
pub fn verify_signature(
    bundle: &Path,
    signature: &Path,
    public_key: Option<&Path>,
) -> Result<(), AppError> {
    if !signature.is_file() {
        return Err(AppError::usage(
            "signature_missing",
            format!("signature file {} does not exist", signature.display()),
        ));
    }
    if let Some(pk) = public_key {
        if !pk.is_file() {
            return Err(AppError::usage(
                "signature_public_key_missing",
                format!("public key file {} does not exist", pk.display()),
            ));
        }
    }
    let minisign = crate::adapters::cli::which("minisign").ok_or_else(|| {
        AppError::integrity(
            "minisign_not_installed",
            "signature verification requested, but 'minisign' is not on PATH; \
             fail-closed (install minisign, or verify without --signature)",
        )
    })?;
    let mut cmd = std::process::Command::new(&minisign);
    cmd.arg("-V")
        .arg("-m")
        .arg(bundle.join("MANIFEST.json"))
        .arg("-x")
        .arg(signature);
    if let Some(pk) = public_key {
        cmd.arg("-p").arg(pk);
    }
    let out = cmd.output().map_err(|e| {
        AppError::integrity("minisign_failed", format!("cannot run minisign: {e}"))
    })?;
    if !out.status.success() {
        let stdout = String::from_utf8_lossy(&out.stdout);
        let stderr = String::from_utf8_lossy(&out.stderr);
        let mut detail = String::new();
        for chunk in [stdout.trim(), stderr.trim()] {
            if !chunk.is_empty() {
                if !detail.is_empty() {
                    detail.push('\n');
                }
                detail.push_str(chunk);
            }
        }
        return Err(AppError::integrity(
            "bundle_signature_invalid",
            format!(
                "minisign rejected the signature over MANIFEST.json: {}",
                detail.chars().take(400).collect::<String>()
            ),
        ));
    }
    Ok(())
}

fn validate_evidence_closure(
    entries: &[Value],
    manifest: &Value,
    pipeline: &Value,
    failures: &mut Vec<String>,
) {
    let Some(stages) = pipeline.get("stages").and_then(Value::as_array) else {
        return;
    };
    let mut declared = BTreeMap::new();
    for stage in stages {
        let Some(id) = stage.get("id").and_then(Value::as_str) else {
            continue;
        };
        declared.insert(id, stage);
    }

    for entry in entries {
        if matches!(
            entry.get("kind").and_then(Value::as_str),
            Some("artifact" | "receipt" | "gate_record")
        ) {
            let stage = entry.get("stage").and_then(Value::as_str).unwrap_or("");
            if !declared.contains_key(stage) {
                failures.push(format!(
                    "{} entry references undeclared stage '{stage}'",
                    entry
                        .get("kind")
                        .and_then(Value::as_str)
                        .unwrap_or("evidence")
                ));
            }
        }
    }

    let gate_log = manifest
        .get("gate_log")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    for log in gate_log {
        let stage = log.get("stage").and_then(Value::as_str).unwrap_or("");
        if !declared.contains_key(stage) {
            failures.push(format!("gate_log references undeclared stage '{stage}'"));
        }
    }

    for (stage_id, stage) in declared {
        let evidence = |kind: &str| {
            entries
                .iter()
                .filter(|entry| {
                    entry.get("kind").and_then(Value::as_str) == Some(kind)
                        && entry.get("stage").and_then(Value::as_str) == Some(stage_id)
                })
                .count()
        };
        let declared_outputs = stage
            .get("out")
            .and_then(Value::as_array)
            .map(Vec::len)
            .unwrap_or(0);
        let artifacts = evidence("artifact");
        if artifacts != declared_outputs {
            failures.push(format!(
                "stage '{stage_id}' declares {declared_outputs} output(s) but bundle carries {artifacts} artifact entry/entries"
            ));
        }
        let receipts = evidence("receipt");
        if receipts == 0 {
            failures.push(format!("stage '{stage_id}' has no receipt evidence"));
        }

        let gate_records = evidence("gate_record");
        let logs: Vec<&Value> = gate_log
            .iter()
            .filter(|log| log.get("stage").and_then(Value::as_str) == Some(stage_id))
            .collect();
        match stage.get("gate").and_then(Value::as_str) {
            Some(gate_id) => {
                if gate_records != 1 {
                    failures.push(format!(
                        "stage '{stage_id}' declares gate '{gate_id}' but bundle carries {gate_records} gate record(s)"
                    ));
                }
                if logs.len() != 1
                    || logs[0].get("gate_id").and_then(Value::as_str) != Some(gate_id)
                {
                    failures.push(format!(
                        "stage '{stage_id}' declares gate '{gate_id}' but gate_log has no unique matching entry"
                    ));
                }
            }
            None => {
                if gate_records != 0 || !logs.is_empty() {
                    failures.push(format!(
                        "stage '{stage_id}' declares no gate but bundle carries gate evidence"
                    ));
                }
            }
        }
    }
}

fn validate_receipt_binding(
    revision: &str,
    receipt: &Value,
    receipt_entry: &Value,
    entries: &[Value],
    manifest: &Value,
    failures: &mut Vec<String>,
) {
    let rel = receipt_entry
        .get("path")
        .and_then(Value::as_str)
        .unwrap_or("<receipt>");
    let stage = receipt_entry
        .get("stage")
        .and_then(Value::as_str)
        .unwrap_or("");
    if let Some(claimed_stage) = receipt.get("stage").and_then(Value::as_str) {
        if claimed_stage != stage {
            failures.push(format!(
                "receipt '{rel}' names stage '{claimed_stage}', manifest entry names '{stage}'"
            ));
        }
    }

    if revision == "highball.action-packet.v1" {
        if let Some(packet_digest) = receipt.get("packet_sha256").and_then(Value::as_str) {
            let bound = entries.iter().any(|entry| {
                entry.get("kind").and_then(Value::as_str) == Some("artifact")
                    && entry.get("stage").and_then(Value::as_str) == Some(stage)
                    && entry.get("sha256").and_then(Value::as_str) == Some(packet_digest)
            });
            if !bound {
                failures.push(format!(
                    "HIGHBALL receipt '{rel}' packet_sha256 {packet_digest} does not bind a same-stage artifact"
                ));
            }
        }
    }

    if revision == "doctrine.brief.v0" {
        if receipt.get("pack_sha256").and_then(Value::as_str)
            != manifest
                .pointer("/doctrine/resolved_sha256")
                .and_then(Value::as_str)
        {
            failures.push(format!(
                "doctrine receipt '{rel}' pack digest does not match bundle provenance"
            ));
        }
        if let Some(brief_digest) = receipt.get("brief_sha256").and_then(Value::as_str) {
            let bound = entries.iter().any(|entry| {
                entry.get("kind").and_then(Value::as_str) == Some("artifact")
                    && entry.get("stage").and_then(Value::as_str) == Some(stage)
                    && entry.get("sha256").and_then(Value::as_str) == Some(brief_digest)
            });
            if !bound {
                failures.push(format!(
                    "doctrine receipt '{rel}' brief_sha256 does not bind a same-stage artifact"
                ));
            }
        }
    }
}

fn safe_bundle_path(rel: &str) -> bool {
    if rel.is_empty() || rel.contains('\\') {
        return false;
    }
    let path = Path::new(rel);
    !path.is_absolute()
        && path.components().all(|component| match component {
            Component::Normal(name) => name
                .to_str()
                .is_some_and(|part| part != "." && part != ".."),
            _ => false,
        })
}

fn bundle_files(bundle: &Path) -> Result<BTreeSet<String>, AppError> {
    fn walk(base: &Path, dir: &Path, out: &mut BTreeSet<String>) -> Result<(), AppError> {
        for entry in std::fs::read_dir(dir)? {
            let entry = entry?;
            let path = entry.path();
            let ty = entry.file_type()?;
            if ty.is_symlink() || (!ty.is_file() && !ty.is_dir()) {
                return Err(AppError::integrity(
                    "bundle_invalid",
                    format!("bundle contains unsupported entry {}", path.display()),
                ));
            }
            if ty.is_dir() {
                walk(base, &path, out)?;
            } else {
                let rel = path
                    .strip_prefix(base)
                    .map_err(|e| AppError::internal(format!("bundle relative path failed: {e}")))?;
                let rel = rel.to_str().ok_or_else(|| {
                    AppError::integrity("bundle_invalid", "bundle contains a non-UTF-8 path")
                })?;
                out.insert(rel.replace('\\', "/"));
            }
        }
        Ok(())
    }

    let mut files = BTreeSet::new();
    walk(bundle, bundle, &mut files)?;
    Ok(files)
}

fn reevaluate_gate(
    bundle: &Path,
    entries: &[Value],
    def: &gates::GateDef,
    record: &Value,
    stage: &str,
    failures: &mut Vec<String>,
) {
    // The record pins exactly what the gate read (artifact_sha256); resolve
    // the same evidence bytes from the bundle by digest.
    let evidence_digest = record
        .get("artifact_sha256")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let resolve_artifact = {
        let bundle = bundle.to_path_buf();
        let entries: Vec<Value> = entries.to_vec();
        let want = evidence_digest.clone();
        move |_name: &str| -> Result<(String, Vec<u8>), AppError> {
            for entry in &entries {
                if entry.get("sha256").and_then(Value::as_str) == Some(want.as_str()) {
                    let rel = entry
                        .get("path")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            AppError::integrity(
                                "gate_artifact_missing",
                                format!(
                                    "entry with digest {want} has a non-string 'path'"
                                ),
                            )
                        })?;
                    return Ok((want.clone(), std::fs::read(bundle.join(rel))?));
                }
            }
            Err(AppError::integrity(
                "gate_artifact_missing",
                format!("no bundle entry with digest {want}"),
            ))
        }
    };
    let receipts: Vec<(String, Vec<u8>)> = entries
        .iter()
        .filter(|e| {
            e.get("kind").and_then(Value::as_str) == Some("receipt")
                && e.get("stage").and_then(Value::as_str) == Some(stage)
        })
        .filter_map(|e| {
            let rel = e["path"].as_str()?;
            let digest = e["sha256"].as_str()?.to_string();
            std::fs::read(bundle.join(rel)).ok().map(|b| (digest, b))
        })
        .collect();

    match gates::evaluate(def, &resolve_artifact, &receipts, &bundle.join("doctrine")) {
        Err(e) => failures.push(format!("gate '{}' re-evaluation error: {e}", def.id)),
        Ok(outcome) => {
            let recorded_decision = record.get("decision").and_then(Value::as_str);
            if Some(outcome.decision) != recorded_decision {
                failures.push(format!(
                    "gate '{}' re-evaluates to '{}' but record says '{:?}'",
                    def.id, outcome.decision, recorded_decision
                ));
            }
            let recorded_observed = record.get("observed").cloned().unwrap_or(Value::Null);
            let recomputed_observed = outcome.observed.clone().unwrap_or(Value::Null);
            if canon::canonical(&recomputed_observed) != canon::canonical(&recorded_observed) {
                failures.push(format!(
                    "gate '{}' observed value re-evaluates to {} but record says {}",
                    def.id,
                    canon::canonical(&recomputed_observed),
                    canon::canonical(&recorded_observed)
                ));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn verify_reports_malformed_manifest_paths_instead_of_panicking() {
        // A third-party bundle may carry a MANIFEST.json whose entries
        // violate the bundle schema (e.g. a numeric `path`). The schema
        // pass only accumulates failures, so the later receipt/gate
        // walks must treat non-string paths as failures, never panic.
        let dir = std::env::temp_dir().join(format!(
            "stammtisch-bundle-{}",
            crate::ids::uuid_v7().unwrap()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let manifest = serde_json::json!({
            "manifest_version": "3.0",
            "run_id": "00000000-0000-7000-8000-000000000001",
            "created_at": "2026-08-26T00:00:00Z",
            "pipeline": {"id": "p", "canonical_sha256": "sha256:00"},
            "doctrine": {"pack": "std", "resolved_sha256": "sha256:00"},
            "entries": [
                {"path": 12345, "kind": "receipt", "sha256": "sha256:00", "stage": "s1"},
                {"path": 67890, "kind": "gate_record", "sha256": "sha256:00", "stage": "s1"},
            ],
        });
        std::fs::write(
            dir.join("MANIFEST.json"),
            serde_json::to_string(&manifest).unwrap(),
        )
        .unwrap();
        let (pass, report) = verify(&dir).unwrap();
        assert!(!pass, "malformed manifest must not pass verification");
        let failures = report
            .get("failures")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let text = format!("{failures:?}");
        assert!(
            text.contains("receipt entry 'path' is not a string"),
            "expected a receipt path failure, got: {text}"
        );
        assert!(
            text.contains("gate record entry 'path' is not a string"),
            "expected a gate record path failure, got: {text}"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn find_receipt_path_scans_beyond_sixteen() {
        let dir = std::env::temp_dir().join(format!(
            "stammtisch-bundle-{}",
            crate::ids::uuid_v7().unwrap()
        ));
        std::fs::create_dir_all(dir.join("receipts")).unwrap();
        let mut last_digest = String::new();
        for n in 0..17 {
            let bytes = format!(r#"{{"n":{n},"schema":"doctrine.brief.v0"}}"#);
            if n == 16 {
                last_digest = canon::sha256_prefixed(bytes.as_bytes());
            }
            std::fs::write(
                dir.join("receipts").join(format!("review.{n}.json")),
                bytes,
            )
            .unwrap();
        }
        let rel = find_receipt_path(&dir, "review", &last_digest).unwrap();
        assert_eq!(rel, "receipts/review.16.json");
        std::fs::remove_dir_all(&dir).ok();
    }

    // ------------------------------------------------------ P3 signature

    /// Temporarily replace PATH so the minisign lookup resolves (or fails
    /// to resolve) deterministically. PATH is process-global, so tests
    /// that touch it are serialized by a mutex.
    struct PathGuard {
        _lock: std::sync::MutexGuard<'static, ()>,
        old: Option<std::ffi::OsString>,
    }

    static PATH_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    impl PathGuard {
        fn set(dir: &Path) -> Self {
            let lock = PATH_LOCK.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            let old = std::env::var_os("PATH");
            std::env::set_var("PATH", dir);
            PathGuard { _lock: lock, old }
        }
    }

    impl Drop for PathGuard {
        fn drop(&mut self) {
            match self.old.take() {
                Some(old) => std::env::set_var("PATH", old),
                None => std::env::remove_var("PATH"),
            }
        }
    }

    #[test]
    fn verify_signature_requires_the_signature_file() {
        let err = verify_signature(Path::new("/nonexistent-bundle"), Path::new("/nonexistent.sig"), None)
            .unwrap_err();
        assert_eq!(err.code, "signature_missing");
    }

    #[test]
    fn verify_signature_fails_closed_when_minisign_absent() {
        let dir = std::env::temp_dir().join(format!(
            "stammtisch-signature-{}",
            crate::ids::uuid_v7().unwrap()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let sig = dir.join("MANIFEST.json.minisig");
        std::fs::write(&sig, b"signature").unwrap();
        let empty_path = dir.join("empty-path");
        std::fs::create_dir_all(&empty_path).unwrap();
        let _guard = PathGuard::set(&empty_path);
        let err = verify_signature(&dir, &sig, None).unwrap_err();
        assert_eq!(
            err.code, "minisign_not_installed",
            "a requested signature without minisign must fail closed, never skip"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[cfg(unix)]
    fn fake_minisign(bin_dir: &Path, args_file: &Path, exit_code: i32) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        std::fs::create_dir_all(bin_dir).unwrap();
        let script = bin_dir.join("minisign");
        std::fs::write(
            &script,
            format!(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > {}\necho 'Signature check failed: forged' >&2\nexit {exit_code}\n",
                args_file.display()
            ),
        )
        .unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
        script
    }

    #[cfg(unix)]
    #[test]
    fn verify_signature_invokes_minisign_with_expected_args() {
        let dir = std::env::temp_dir().join(format!(
            "stammtisch-signature-{}",
            crate::ids::uuid_v7().unwrap()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let manifest = dir.join("MANIFEST.json");
        std::fs::write(&manifest, b"{}").unwrap();
        let sig = dir.join("MANIFEST.json.minisig");
        std::fs::write(&sig, b"signature").unwrap();
        let pk = dir.join("pubkey.minisign.pub");
        std::fs::write(&pk, b"untrusted comment: fake key\n").unwrap();
        let args_file = dir.join("args.txt");
        let _script = fake_minisign(&dir.join("bin"), &args_file, 0);

        // With an explicit public key.
        {
            let _guard = PathGuard::set(&dir.join("bin"));
            verify_signature(&dir, &sig, Some(&pk)).unwrap();
        }
        let got: Vec<String> = std::fs::read_to_string(&args_file)
            .unwrap()
            .lines()
            .map(str::to_string)
            .collect();
        assert_eq!(
            got,
            vec![
                "-V",
                "-m",
                manifest.to_str().unwrap(),
                "-x",
                sig.to_str().unwrap(),
                "-p",
                pk.to_str().unwrap(),
            ]
        );

        // Without a public key, minisign falls back to its own default
        // key lookup (cwd `minisign.pub`); the hook passes no -p.
        std::fs::remove_file(&args_file).unwrap();
        {
            let _guard = PathGuard::set(&dir.join("bin"));
            verify_signature(&dir, &sig, None).unwrap();
        }
        let got: Vec<String> = std::fs::read_to_string(&args_file)
            .unwrap()
            .lines()
            .map(str::to_string)
            .collect();
        assert_eq!(
            got,
            vec!["-V", "-m", manifest.to_str().unwrap(), "-x", sig.to_str().unwrap()]
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    #[cfg(unix)]
    #[test]
    fn verify_signature_rejects_when_minisign_verification_fails() {
        let dir = std::env::temp_dir().join(format!(
            "stammtisch-signature-{}",
            crate::ids::uuid_v7().unwrap()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let manifest = dir.join("MANIFEST.json");
        std::fs::write(&manifest, b"{}").unwrap();
        let sig = dir.join("MANIFEST.json.minisig");
        std::fs::write(&sig, b"forged").unwrap();
        let args_file = dir.join("args.txt");
        let _script = fake_minisign(&dir.join("bin"), &args_file, 1);
        let _guard = PathGuard::set(&dir.join("bin"));
        let err = verify_signature(&dir, &sig, None).unwrap_err();
        assert_eq!(err.code, "bundle_signature_invalid");
        assert!(
            err.message.contains("Signature check failed"),
            "rejection detail surfaced: {}",
            err.message
        );
        std::fs::remove_dir_all(&dir).ok();
    }
}
