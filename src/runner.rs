//! The run state machine (architecture doc §4) and its event-sourced
//! projection. `events.jsonl` is the authority; `manifest.json` is a pure
//! fold over it, rewritten after every fsynced event and fully rebuildable
//! at any time.

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::{json, Value};

use crate::adapters::{self, PollState, StageContext, Verdict};
use crate::canon;
use crate::doctrine::{self, DoctrinePack};
use crate::error::AppError;
use crate::gates;
use crate::ids;
use crate::pipeline::{self, Pipeline};
use crate::store::{append_line_fsync, atomic_write, EventWriter, LaunchLock, StateRoot};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Terminal {
    Completed,
    Blocked,
    Halted,
    Failed,
}

impl Terminal {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Completed => "completed",
            Self::Blocked => "blocked",
            Self::Halted => "halted",
            Self::Failed => "failed",
        }
    }

    /// CLI exit code for `run` per architecture doc §8.
    pub fn exit_code(&self) -> i32 {
        match self {
            Self::Completed => 0,
            Self::Failed => 1,
            Self::Blocked | Self::Halted => 2,
        }
    }
}

#[derive(Debug)]
pub struct RunReport {
    pub run_id: String,
    pub terminal: Terminal,
    pub pipeline_id: String,
    pub bundle_manifest_sha256: Option<String>,
    pub detail: String,
}

/// Execute a pipeline end to end. Pre-launch contract failures (invalid
/// spec, doctrine drift, unknown gate id) return `Err` before any run dir
/// exists; once launched, every outcome — including refusal and halt — is
/// recorded durably and reported as `Ok(RunReport)`.
pub fn run_pipeline(root: &StateRoot, pipeline_path: &Path) -> Result<RunReport, AppError> {
    if !root.is_initialized() {
        root.init()?;
    }
    let pipeline = pipeline::load(pipeline_path)?;
    let doctrine = doctrine::resolve(&pipeline, &root.path)?;
    preflight_gates(&pipeline, &doctrine)?;

    let run_id = ids::uuid_v7()?;
    // One active run per state root; refusal is clean and immediate.
    let _lock = LaunchLock::acquire(root, &run_id)?;

    let run_dir = root.run_dir(&run_id);
    for d in ["receipts", "artifacts", "gates"] {
        std::fs::create_dir_all(run_dir.join(d))?;
    }
    // The canonical spec binds the run; the bundle includes it at completion.
    atomic_write(
        &run_dir.join("pipeline.json"),
        &canon::canonical_bytes(&pipeline.value),
    )?;

    let mut w = EventWriter::new(&run_dir, &run_id);
    let stage_list: Vec<Value> = pipeline
        .stages
        .iter()
        .map(|s| {
            json!({
                "id": s.id,
                "product": s.product,
                // Keep the contract metadata needed to prove a completed run
                // from events alone.
                "gate": s.gate,
                "outputs": s.outputs,
            })
        })
        .collect();
    w.emit(
        "run.created",
        None,
        json!({
            "pipeline": {"id": pipeline.id, "canonical_sha256": pipeline.canonical_sha256},
            "doctrine": doctrine_provenance(&pipeline, &doctrine),
            "stages": stage_list,
            "state_root": root.path.display().to_string(),
        }),
    )?;
    rewrite_manifest(&run_dir)?;
    w.emit("run.staged", None, json!({}))?;
    rewrite_manifest(&run_dir)?;

    // artifact name -> digest, accumulated across stages
    let mut produced: BTreeMap<String, String> = BTreeMap::new();
    let mut timings: Vec<crate::cost::StageTiming> = Vec::new();
    let outcome = execute_stages(
        &mut w,
        &run_dir,
        &run_id,
        &pipeline,
        &doctrine,
        &mut produced,
        &mut timings,
    );
    // Cost accounting is fail-safe by design (roadmap P3): a ledger
    // failure never blocks or alters the run outcome.
    let _ = crate::cost::write_ledger(&run_dir, &run_id, &pipeline, &timings);

    match outcome {
        Ok(StageFailureOr::AllPassed) => {
            w.emit("run.gating", None, json!({}))?;
            rewrite_manifest(&run_dir)?;
            let bundle_digest =
                match crate::bundle::assemble(&run_dir, &run_id, &pipeline, &doctrine) {
                    Ok(digest) => digest,
                    Err(e) => {
                        // Bundle assembly is part of completion, not work after
                        // completion. Preserve a durable terminal record instead
                        // of leaving the run stranded in `gating`.
                        w.emit(
                            "run.halted",
                            None,
                            json!({
                                "reason": "bundle_assembly_failed",
                                "summary": e.message,
                            }),
                        )?;
                        rewrite_manifest(&run_dir)?;
                        return Ok(RunReport {
                            run_id,
                            terminal: Terminal::Halted,
                            pipeline_id: pipeline.id.clone(),
                            bundle_manifest_sha256: None,
                            detail: e.message,
                        });
                    }
                };
            let completed_at = crate::time::now_rfc3339();
            w.emit(
                "run.completed",
                None,
                json!({
                    "bundle_manifest_sha256": bundle_digest.clone(),
                    "completed_at": completed_at,
                }),
            )?;
            rewrite_manifest(&run_dir)?;
            Ok(RunReport {
                run_id,
                terminal: Terminal::Completed,
                pipeline_id: pipeline.id.clone(),
                bundle_manifest_sha256: Some(bundle_digest),
                detail: "all stages passed; bundle assembled".into(),
            })
        }
        Ok(StageFailureOr::Terminal {
            terminal,
            stage,
            event_type,
            payload,
        }) => {
            let stage_arg = if event_type.starts_with("stage.") {
                Some(stage.as_str())
            } else {
                None
            };
            w.emit(&event_type, stage_arg, payload.clone())?;
            let run_event = match terminal {
                Terminal::Blocked => "run.blocked",
                Terminal::Halted => "run.halted",
                Terminal::Failed => "run.failed",
                Terminal::Completed => unreachable!("completed handled above"),
            };
            let summary = payload
                .get("detail")
                .and_then(Value::as_str)
                .unwrap_or("terminal")
                .to_string();
            w.emit(
                run_event,
                None,
                json!({"stage": stage, "reason": payload.get("reason").cloned().unwrap_or(json!("terminal")), "summary": summary}),
            )?;
            rewrite_manifest(&run_dir)?;
            Ok(RunReport {
                run_id,
                terminal,
                pipeline_id: pipeline.id.clone(),
                bundle_manifest_sha256: None,
                detail: format!("stage '{stage}': {summary}"),
            })
        }
        Err(e) => {
            // Internal/io failure mid-run: still leave a durable record.
            let _ = w.emit(
                "run.halted",
                None,
                json!({"reason": "internal_error", "summary": e.message}),
            );
            let _ = rewrite_manifest(&run_dir);
            Err(e)
        }
    }
}

enum StageFailureOr {
    AllPassed,
    Terminal {
        terminal: Terminal,
        stage: String,
        /// Stage-level event to record before the run-level terminal event
        /// ("stage.failed" / "stage.gate_failed"); empty for none.
        event_type: String,
        payload: Value,
    },
}

fn execute_stages(
    w: &mut EventWriter,
    run_dir: &Path,
    run_id: &str,
    pipeline: &Pipeline,
    doctrine: &DoctrinePack,
    produced: &mut BTreeMap<String, String>,
    timings: &mut Vec<crate::cost::StageTiming>,
) -> Result<StageFailureOr, AppError> {
    for stage in &pipeline.stages {
        // Product-contact wall time is runner-observed cost evidence
        // (roadmap P3): preflight through collect, recorded on every
        // outcome path — a failing stage still shows what it cost.
        let contact_started = std::time::Instant::now();
        let outcome = execute_one_stage(w, run_dir, run_id, pipeline, doctrine, produced, stage);
        timings.push(crate::cost::StageTiming {
            stage: stage.id.clone(),
            wall_seconds: contact_started.elapsed().as_secs_f64(),
        });
        match outcome {
            Ok(StageFailureOr::AllPassed) => {}
            other => return other,
        }
    }
    Ok(StageFailureOr::AllPassed)
}

/// Drive one stage through preflight/invoke/poll/collect, persist its
/// receipts and artifacts, and evaluate its gate. Every early return is a
/// terminal (or internal-error) outcome; the caller records the timing.
fn execute_one_stage(
    w: &mut EventWriter,
    run_dir: &Path,
    run_id: &str,
    pipeline: &Pipeline,
    doctrine: &DoctrinePack,
    produced: &mut BTreeMap<String, String>,
    stage: &pipeline::Stage,
) -> Result<StageFailureOr, AppError> {
    w.emit(
        "stage.started",
        Some(&stage.id),
        json!({"product": stage.product}),
    )?;
    rewrite_manifest(run_dir)?;

    let inputs = match bind_stage_inputs(stage, produced, run_dir) {
            Ok(inputs) => inputs,
            Err(e) => {
                return Ok(terminal(Terminal::Halted, stage, e.code, e.message));
            }
        };
        let adapter = match adapters::for_stage(stage) {
            Ok(adapter) => adapter,
            Err(e) => {
                return Ok(terminal(
                    adapters::failure_terminal(&e),
                    stage,
                    e.code,
                    e.message,
                ));
            }
        };
        let ctx = StageContext {
            run_id,
            pipeline_id: &pipeline.id,
            stage,
            doctrine,
            inputs: &inputs,
            run_dir,
        };
        if let Err(e) = adapter.preflight(&ctx) {
            return stage_terminal(
                w,
                run_dir,
                adapter.as_ref(),
                stage,
                adapters::failure_terminal(&e),
                "preflight_failed",
                e.message,
            );
        }
        let handle = match adapter.invoke(&ctx) {
            Ok(h) => h,
            Err(e) => {
                return stage_terminal(
                    w,
                    run_dir,
                    adapter.as_ref(),
                    stage,
                    adapters::failure_terminal(&e),
                    "invoke_failed",
                    e.message,
                );
            }
        };
        match adapter.poll(&handle) {
            PollState::Completed => {}
            PollState::Failed(reason) => {
                return stage_terminal(
                    w,
                    run_dir,
                    adapter.as_ref(),
                    stage,
                    Terminal::Failed,
                    "poll_failed",
                    reason,
                );
            }
            PollState::Halted(reason) => {
                return stage_terminal(
                    w,
                    run_dir,
                    adapter.as_ref(),
                    stage,
                    Terminal::Halted,
                    "poll_halted",
                    reason,
                );
            }
        }
        let collected = match adapter.collect(&handle, &ctx) {
            Ok(c) => c,
            Err(e) => {
                return stage_terminal(
                    w,
                    run_dir,
                    adapter.as_ref(),
                    stage,
                    adapters::failure_terminal(&e),
                    "collect_failed",
                    e.message,
                );
            }
        };

        // Receipts: contract-validated before acceptance, fail-closed.
        let accepted_receipts: Vec<(String, Vec<u8>)> =
            match persist_receipts(w, run_dir, stage, &collected.receipts)? {
                Ok(accepted) => accepted,
                Err(rejected) => {
                    return Ok(terminal(
                        Terminal::Halted,
                        stage,
                        "receipt_rejected",
                        rejected,
                    ));
                }
            };
        rewrite_manifest(run_dir)?;

        // Artifacts: content-addressed store, drift-checked on collision.
        let mut collected_names = std::collections::BTreeSet::new();
        for (name, content) in &collected.artifacts {
            if !stage.outputs.iter().any(|declared| declared == name) {
                return Ok(terminal(
                    Terminal::Halted,
                    stage,
                    "stage_output_undeclared",
                    format!("product returned undeclared artifact '{name}'"),
                ));
            }
            if !collected_names.insert(name.clone()) {
                return Ok(terminal(
                    Terminal::Halted,
                    stage,
                    "stage_output_duplicate",
                    format!("product returned artifact '{name}' more than once"),
                ));
            }
            let bytes = canon::canonical_bytes(content);
            let digest = canon::sha256_prefixed(&bytes);
            let hex = &digest["sha256:".len()..];
            let path = run_dir.join("artifacts").join(hex);
            if path.exists() {
                let existing = std::fs::read(&path)?;
                if canon::sha256_prefixed(&existing) != digest {
                    return Ok(terminal(
                        Terminal::Halted,
                        stage,
                        "artifact_digest_drift",
                        format!("content-addressed slot {hex} holds different bytes"),
                    ));
                }
            } else {
                atomic_write(&path, &bytes)?;
            }
            w.emit(
                "stage.artifact_recorded",
                Some(&stage.id),
                json!({"name": name, "digest": digest}),
            )?;
            produced.insert(name.clone(), digest);
        }
        if let Some(missing) = stage
            .outputs
            .iter()
            .find(|declared| !collected_names.contains(*declared))
        {
            return Ok(terminal(
                Terminal::Halted,
                stage,
                "stage_output_missing",
                format!("product did not return declared artifact '{missing}'"),
            ));
        }
        rewrite_manifest(run_dir)?;

        // Product refusal (HIGHBALL DENIED): the
        // stage's on_block policy maps the terminal state (§5.1).
        if let Verdict::Refused(verdict) = &collected.verdict {
            let terminal_state = if stage.on_block == "blocked" {
                Terminal::Blocked
            } else {
                Terminal::Halted
            };
            return Ok(StageFailureOr::Terminal {
                terminal: terminal_state,
                stage: stage.id.clone(),
                event_type: "stage.failed".to_string(),
                payload: json!({
                    "reason": "product_refused",
                    "verdict": verdict,
                    "detail": format!("product refused with verdict {verdict}"),
                }),
            });
        }

        // Gate evaluation — quantified, in code, with a durable record.
        if let Some(gate_id) = &stage.gate {
            let raw = doctrine
                .gate(gate_id)
                .ok_or_else(|| AppError::usage("gate_unknown", format!("gate '{gate_id}'")))?
                .clone();
            let def = match gates::parse_def(&raw) {
                Ok(d) => d,
                Err(e) => {
                    return Ok(terminal(
                        Terminal::Halted,
                        stage,
                        "gate_def_invalid",
                        e.message,
                    ));
                }
            };
            let run_dir_owned = run_dir.to_path_buf();
            let produced_snapshot = produced.clone();
            let resolve_artifact = move |name: &str| -> Result<(String, Vec<u8>), AppError> {
                let digest = produced_snapshot.get(name).cloned().ok_or_else(|| {
                    AppError::integrity(
                        "gate_artifact_missing",
                        format!("gate references unknown artifact '{name}'"),
                    )
                })?;
                let hex = &digest["sha256:".len()..];
                let bytes = std::fs::read(run_dir_owned.join("artifacts").join(hex))?;
                Ok((digest, bytes))
            };
            let outcome =
                gates::evaluate(&def, &resolve_artifact, &accepted_receipts, &doctrine.dir)?;
            let record = gates::build_record(&def, run_id, &stage.id, &outcome);
            let record_bytes = canon::canonical_bytes(&record);
            let record_digest = canon::sha256_prefixed(&record_bytes);
            atomic_write(
                &run_dir
                    .join("gates")
                    .join(format!("{}.gate.json", stage.id)),
                &record_bytes,
            )?;
            let event_payload = json!({
                "gate_id": def.id,
                "decision": outcome.decision,
                "observed": outcome.observed.clone().unwrap_or(Value::Null),
                "record_sha256": record_digest,
                "detail": outcome.detail,
            });
            if outcome.decision == "pass" {
                w.emit("stage.gate_passed", Some(&stage.id), event_payload)?;
                rewrite_manifest(run_dir)?;
            } else {
                let terminal_state = if outcome.effective_on_fail == "blocked" {
                    Terminal::Blocked
                } else {
                    Terminal::Halted
                };
                return Ok(StageFailureOr::Terminal {
                    terminal: terminal_state,
                    stage: stage.id.clone(),
                    event_type: "stage.gate_failed".to_string(),
                    payload: json!({
                        "reason": "gate_failed",
                        "gate_id": def.id,
                        "record_sha256": event_payload["record_sha256"],
                        "detail": outcome.detail,
                    }),
                });
            }
    }
    Ok(StageFailureOr::AllPassed)
}

/// Resolve exactly the artifacts named by `stage.in`. The accumulated
/// `produced` map is runner-private history, not ambient adapter authority:
/// an adapter must never observe an artifact that its stage did not declare.
/// Missing, malformed, unreadable, or digest-drifted declared inputs halt
/// before any product contact.
fn bind_stage_inputs(
    stage: &pipeline::Stage,
    produced: &BTreeMap<String, String>,
    run_dir: &Path,
) -> Result<BTreeMap<String, String>, AppError> {
    let mut inputs = BTreeMap::new();
    for name in &stage.inputs {
        let digest = produced.get(name).ok_or_else(|| {
            AppError::integrity(
                "stage_input_missing",
                format!(
                    "stage '{}' declares input '{name}', but no earlier stage recorded it",
                    stage.id
                ),
            )
        })?;
        let hex = digest.strip_prefix("sha256:").ok_or_else(|| {
            AppError::integrity(
                "stage_input_digest_invalid",
                format!(
                    "stage '{}' input '{name}' has malformed digest '{digest}'",
                    stage.id
                ),
            )
        })?;
        if hex.len() != 64
            || !hex
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        {
            return Err(AppError::integrity(
                "stage_input_digest_invalid",
                format!(
                    "stage '{}' input '{name}' has malformed digest '{digest}'",
                    stage.id
                ),
            ));
        }
        let path = run_dir.join("artifacts").join(hex);
        let bytes = std::fs::read(&path).map_err(|e| {
            AppError::integrity(
                "stage_input_missing",
                format!(
                    "stage '{}' input artifact '{name}' is unreadable: {e}",
                    stage.id
                ),
            )
        })?;
        let actual = canon::sha256_prefixed(&bytes);
        if actual != *digest {
            return Err(AppError::integrity(
                "stage_input_digest_drift",
                format!(
                    "stage '{}' input '{name}' hashes to {actual}, but events pin {digest}",
                    stage.id
                ),
            ));
        }
        serde_json::from_slice::<Value>(&bytes).map_err(|e| {
            AppError::integrity(
                "stage_input_unparseable",
                format!(
                    "stage '{}' input artifact '{name}' is not canonical JSON evidence: {e}",
                    stage.id
                ),
            )
        })?;
        inputs.insert(name.clone(), digest.clone());
    }
    Ok(inputs)
}

fn terminal(
    state: Terminal,
    stage: &pipeline::Stage,
    reason: &str,
    detail: String,
) -> StageFailureOr {
    StageFailureOr::Terminal {
        terminal: state,
        stage: stage.id.clone(),
        event_type: "stage.failed".to_string(),
        payload: json!({"reason": reason, "detail": detail}),
    }
}

/// Contract-validate and persist stage receipts (content-addressed
/// discipline): each receipt is validated before acceptance, written to
/// `receipts/<stage>.<n>.json`, and recorded as a digest event. The first
/// invalid receipt fails closed with its rejection detail.
type AcceptedReceipt = (String, Vec<u8>);
type ReceiptAcceptance = Result<Vec<AcceptedReceipt>, String>;

fn persist_receipts(
    w: &mut EventWriter,
    run_dir: &Path,
    stage: &pipeline::Stage,
    receipts: &[Value],
) -> Result<ReceiptAcceptance, AppError> {
    let mut accepted = Vec::new();
    for (n, receipt) in receipts.iter().enumerate() {
        let bytes = canon::canonical_bytes(receipt);
        if let Err(err) = crate::contracts::validate_receipt(&bytes) {
            return Ok(Err(err.to_string()));
        }
        let digest = canon::sha256_prefixed(&bytes);
        let path = run_dir
            .join("receipts")
            .join(format!("{}.{n}.json", stage.id));
        atomic_write(&path, &bytes)?;
        w.emit(
            "stage.receipt_accepted",
            Some(&stage.id),
            json!({"digest": digest}),
        )?;
        accepted.push((digest, bytes));
    }
    Ok(Ok(accepted))
}

/// Terminal path for a stage: salvage any receipts the adapter accumulated
/// before the failure (real adapters observe preflight/start/status
/// receipts even when the stage never reaches `collect`), persist them with
/// the same fail-closed validation, then report the terminal outcome. A
/// contract-invalid salvaged receipt overrides the outcome with a
/// receipt_rejected halt — integrity outranks the original failure.
fn stage_terminal(
    w: &mut EventWriter,
    run_dir: &Path,
    adapter: &dyn adapters::Adapter,
    stage: &pipeline::Stage,
    state: Terminal,
    reason: &str,
    detail: String,
) -> Result<StageFailureOr, AppError> {
    let drained = adapter.drain_receipts();
    if let Err(rejected) = persist_receipts(w, run_dir, stage, &drained)? {
        return Ok(terminal(
            Terminal::Halted,
            stage,
            "receipt_rejected",
            rejected,
        ));
    }
    if !drained.is_empty() {
        rewrite_manifest(run_dir)?;
    }
    Ok(terminal(state, stage, reason, detail))
}

pub fn preflight_gates(pipeline: &Pipeline, doctrine: &DoctrinePack) -> Result<(), AppError> {
    for stage in &pipeline.stages {
        if let Some(gate_id) = &stage.gate {
            if doctrine.gate(gate_id).is_none() {
                return Err(AppError::usage(
                    "gate_unknown",
                    format!(
                        "stage '{}' references gate '{gate_id}' but pack '{}' does not define it",
                        stage.id, doctrine.name
                    ),
                ));
            }
        }
    }
    Ok(())
}

fn doctrine_provenance(pipeline: &Pipeline, doctrine: &DoctrinePack) -> Value {
    let mut p = json!({
        "pack": doctrine.name,
        "resolved_sha256": doctrine.digest,
        "dir": doctrine.dir.display().to_string(),
    });
    if let Some(r) = &pipeline.doctrine_ref {
        p["ref"] = json!(r);
    }
    p
}

// ------------------------------------------------------------- projection

/// Fold `events.jsonl` into a manifest. Historical logs project to the
/// immutable `stammtisch.manifest.v0`; enriched logs emitted by this runtime
/// project to v1, which adds the event-pinned bundle digest.
pub fn project(events: &[Value]) -> Result<Value, AppError> {
    let mut manifest = json!({
        "schema": "stammtisch.manifest.v0",
        "state": {"code": "created", "current_stage": Value::Null, "blockers": []},
        "stages": [],
    });
    let mut saw_created = false;
    let mut saw_terminal = false;
    let mut next_stage = 0usize;
    // Historical run-event.v0 logs did not carry gate/output declarations in
    // run.created and did not pin the assembled bundle in run.completed.
    // Keep those logs readable under their original contract while applying
    // the stronger completeness proof only to enriched v0 logs emitted now.
    let mut enriched_contract = false;
    for event in events {
        let event_type = event["type"]
            .as_str()
            .ok_or_else(|| AppError::integrity("run_corrupt", "event has no string type"))?;
        if saw_terminal {
            return Err(AppError::integrity(
                "run_corrupt",
                format!("event '{event_type}' appears after a terminal run event"),
            ));
        }
        let payload = &event["payload"];
        match event_type {
            "run.created" => {
                if saw_created {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        "run.created appears more than once",
                    ));
                }
                if event.get("stage").is_some() {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        "run.created must not name a stage",
                    ));
                }
                let stages = payload
                    .get("stages")
                    .and_then(Value::as_array)
                    .ok_or_else(|| {
                        AppError::integrity("run_corrupt", "run.created has no stages array")
                    })?;
                if stages.is_empty() {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        "run.created declares no stages",
                    ));
                }
                let mut declared = std::collections::BTreeSet::new();
                let mut list = Vec::new();
                enriched_contract = stages.iter().all(|stage| {
                    stage.get("outputs").is_some()
                        && stage.get("gate").is_some()
                        && stage.get("id").is_some()
                        && stage.get("product").is_some()
                });
                if enriched_contract {
                    manifest["schema"] = json!("stammtisch.manifest.v1");
                }
                for s in stages {
                    let id = s.get("id").and_then(Value::as_str).ok_or_else(|| {
                        AppError::integrity("run_corrupt", "run.created stage has no string id")
                    })?;
                    let product = s.get("product").and_then(Value::as_str).ok_or_else(|| {
                        AppError::integrity(
                            "run_corrupt",
                            format!("run.created stage '{id}' has no string product"),
                        )
                    })?;
                    if !declared.insert(id.to_string()) {
                        return Err(AppError::integrity(
                            "run_corrupt",
                            format!("run.created declares duplicate stage '{id}'"),
                        ));
                    }
                    list.push(json!({
                        "id": id,
                        "product": product,
                        "status": "pending",
                        "receipts": [],
                        "artifacts": [],
                        "gate_required": s.get("gate").is_some_and(|v| !v.is_null()),
                        "declared_gate": s.get("gate").cloned().unwrap_or(Value::Null),
                        "declared_outputs": s.get("outputs").cloned().unwrap_or(Value::Null),
                    }));
                }
                manifest["run_id"] = event["run_id"].clone();
                manifest["pipeline"] = payload["pipeline"].clone();
                manifest["doctrine"] = strip_doctrine_for_manifest(&payload["doctrine"]);
                manifest["state_root"] = payload["state_root"].clone();
                manifest["created_at"] = event["at"].clone();
                manifest["stages"] = json!(list);
                saw_created = true;
            }
            "run.staged" => {
                require_created(saw_created, event_type)?;
                require_state(&manifest, &["created"], event_type)?;
                manifest["state"]["code"] = json!("staged");
            }
            "stage.started" => {
                require_created(saw_created, event_type)?;
                require_state(&manifest, &["staged", "running"], event_type)?;
                let i = stage_index(&manifest, event)?;
                if i == next_stage + 1 {
                    let previous = &manifest["stages"][next_stage];
                    if previous["status"] != "running" || previous["gate_required"] == true {
                        return Err(AppError::integrity(
                            "run_corrupt",
                            format!(
                                "stage '{}' started before stage '{}' passed",
                                event["stage"].as_str().unwrap_or(""),
                                previous["id"].as_str().unwrap_or("")
                            ),
                        ));
                    }
                    if enriched_contract {
                        validate_stage_evidence(previous)?;
                    }
                    manifest["stages"][next_stage]["status"] = json!("passed");
                    next_stage += 1;
                }
                if i != next_stage {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!(
                            "stage '{}' started out of pipeline order (expected index {next_stage})",
                            event["stage"].as_str().unwrap_or("")
                        ),
                    ));
                }
                if manifest["stages"][i]["status"] != "pending" {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!("stage '{}' started more than once", event["stage"]),
                    ));
                }
                manifest["state"]["code"] = json!("running");
                manifest["state"]["current_stage"] = event["stage"].clone();
                set_stage_status(&mut manifest, event, "running")?;
            }
            "stage.receipt_accepted" => {
                require_active_stage(&manifest, event, "stage.receipt_accepted")?;
                push_stage_digest(&mut manifest, event, "receipts", payload)?;
            }
            "stage.artifact_recorded" => {
                require_active_stage(&manifest, event, "stage.artifact_recorded")?;
                push_stage_digest(&mut manifest, event, "artifacts", payload)?;
            }
            "stage.gate_passed" => {
                let i = require_active_stage(&manifest, event, "stage.gate_passed")?;
                require_digest(payload, "record_sha256", "stage.gate_passed")?;
                if enriched_contract {
                    validate_gate_event(&manifest["stages"][i], payload, "stage.gate_passed")?;
                    validate_stage_evidence(&manifest["stages"][i])?;
                }
                set_stage_status(&mut manifest, event, "passed")?;
                set_stage_gate_record(&mut manifest, event, payload)?;
                next_stage = i + 1;
            }
            "stage.gate_failed" => {
                let i = require_active_stage(&manifest, event, "stage.gate_failed")?;
                require_digest(payload, "record_sha256", "stage.gate_failed")?;
                if enriched_contract {
                    validate_gate_event(&manifest["stages"][i], payload, "stage.gate_failed")?;
                }
                set_stage_status(&mut manifest, event, "refused")?;
                set_stage_gate_record(&mut manifest, event, payload)?;
            }
            "stage.failed" => {
                require_active_stage(&manifest, event, "stage.failed")?;
                let status =
                    if payload.get("reason").and_then(Value::as_str) == Some("product_refused") {
                        "refused"
                    } else {
                        "failed"
                    };
                set_stage_status(&mut manifest, event, status)?;
            }
            "run.gating" => {
                require_created(saw_created, event_type)?;
                require_state(&manifest, &["running"], event_type)?;
                let stages = manifest["stages"]
                    .as_array()
                    .expect("projection stages array");
                if next_stage == stages.len() {
                    // The final stage's gate already advanced it to passed.
                } else if next_stage + 1 == stages.len() {
                    let final_stage = stages.last().expect("non-empty stages");
                    if final_stage["status"] != "running" {
                        return Err(AppError::integrity(
                            "run_corrupt",
                            "run.gating requires the final stage to be running",
                        ));
                    }
                    if enriched_contract && final_stage["gate_required"] == true {
                        return Err(AppError::integrity(
                            "run_corrupt",
                            "run.gating appears before the final stage gate passed",
                        ));
                    }
                    if enriched_contract {
                        validate_stage_evidence(final_stage)?;
                    }
                    manifest["stages"][next_stage]["status"] = json!("passed");
                    next_stage += 1;
                } else {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        "run.gating appears before the final stage",
                    ));
                }
                manifest["state"]["code"] = json!("gating");
                manifest["state"]["current_stage"] = Value::Null;
            }
            "run.completed" => {
                require_created(saw_created, event_type)?;
                require_state(&manifest, &["gating"], event_type)?;
                let stages = manifest["stages"]
                    .as_array()
                    .expect("projection stages array");
                if next_stage != stages.len()
                    || stages.iter().any(|stage| stage["status"] != "passed")
                {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        "run.completed requires every declared stage to have passed",
                    ));
                }
                manifest["state"]["code"] = json!("completed");
                manifest["state"]["current_stage"] = Value::Null;
                if enriched_contract {
                    let bundle_digest =
                        require_digest(payload, "bundle_manifest_sha256", "run.completed")?;
                    manifest["bundle_manifest_sha256"] = json!(bundle_digest);
                } else if payload.get("bundle_manifest_sha256").is_some() {
                    // v0-contract logs (written before the enriched
                    // run.created) may carry the digest in run.completed:
                    // validate it when present but never project it — the
                    // v0 manifest schema forbids the field, and the bundle
                    // manifest pins its own digest.
                    require_digest(payload, "bundle_manifest_sha256", "run.completed")?;
                }
                if let Some(at) = payload.get("completed_at") {
                    manifest["completed_at"] = at.clone();
                } else {
                    manifest["completed_at"] = event["at"].clone();
                }
                saw_terminal = true;
            }
            "run.blocked" | "run.halted" | "run.failed" | "run.cancelled" => {
                require_created(saw_created, event_type)?;
                let state = manifest["state"]["code"].as_str().unwrap_or("created");
                if state == "created" && event_type != "run.cancelled" {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!("{event_type} is invalid before run.staged"),
                    ));
                }
                if matches!(event_type, "run.blocked" | "run.failed") {
                    require_state(&manifest, &["running"], event_type)?;
                    let active = manifest["state"]["current_stage"].as_str().ok_or_else(|| {
                        AppError::integrity(
                            "run_corrupt",
                            format!("{event_type} has no active stage"),
                        )
                    })?;
                    let declared =
                        payload
                            .get("stage")
                            .and_then(Value::as_str)
                            .ok_or_else(|| {
                                AppError::integrity(
                                    "run_corrupt",
                                    format!("{event_type} has no payload.stage"),
                                )
                            })?;
                    if declared != active {
                        return Err(AppError::integrity(
                            "run_corrupt",
                            format!(
                                "{event_type} names stage '{declared}', but active stage is '{active}'"
                            ),
                        ));
                    }
                    let i = manifest["stages"]
                        .as_array()
                        .and_then(|stages| stages.iter().position(|s| s["id"] == declared))
                        .expect("active projected stage exists");
                    let expected_status = if event_type == "run.blocked" {
                        "refused"
                    } else {
                        "failed"
                    };
                    if manifest["stages"][i]["status"] != expected_status {
                        return Err(AppError::integrity(
                            "run_corrupt",
                            format!(
                                "{event_type} requires stage '{declared}' status '{expected_status}'"
                            ),
                        ));
                    }
                }
                if event_type == "run.halted" && !matches!(state, "staged" | "running" | "gating") {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!("run.halted is invalid while run state is '{state}'"),
                    ));
                }
                if event_type == "run.cancelled"
                    && !matches!(state, "created" | "staged" | "running" | "gating")
                {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!("run.cancelled is invalid while run state is '{state}'"),
                    ));
                }
                manifest["state"]["code"] = json!(event_type.trim_start_matches("run."));
                manifest["state"]["current_stage"] = Value::Null;
                let summary = payload
                    .get("summary")
                    .and_then(Value::as_str)
                    .unwrap_or(event_type);
                manifest["state"]["blockers"] = json!([summary]);
                if event_type != "run.cancelled" {
                    manifest["completed_at"] = event["at"].clone();
                }
                saw_terminal = true;
            }
            "run.reconciled" | "run.resumed" => {
                require_created(saw_created, event_type)?;
            }
            other => {
                return Err(AppError::integrity(
                    "run_corrupt",
                    format!("unknown event type '{other}' in event log"),
                ));
            }
        }
    }
    if !saw_created {
        return Err(AppError::integrity(
            "run_corrupt",
            "event log has no run.created event",
        ));
    }
    strip_projection_metadata(&mut manifest);
    Ok(manifest)
}

fn require_created(saw_created: bool, event_type: &str) -> Result<(), AppError> {
    if saw_created {
        Ok(())
    } else {
        Err(AppError::integrity(
            "run_corrupt",
            format!("{event_type} appears before run.created"),
        ))
    }
}

fn require_state(manifest: &Value, allowed: &[&str], event_type: &str) -> Result<(), AppError> {
    let state = manifest["state"]["code"].as_str().unwrap_or("unknown");
    if allowed.contains(&state) {
        Ok(())
    } else {
        Err(AppError::integrity(
            "run_corrupt",
            format!("{event_type} is invalid while run state is '{state}'"),
        ))
    }
}

fn require_active_stage(
    manifest: &Value,
    event: &Value,
    event_type: &str,
) -> Result<usize, AppError> {
    require_state(manifest, &["running"], event_type)?;
    let i = stage_index(manifest, event)?;
    if manifest["stages"][i]["status"] != "running"
        || manifest["state"]["current_stage"] != event["stage"]
    {
        return Err(AppError::integrity(
            "run_corrupt",
            format!(
                "{event_type} references inactive stage '{}'",
                event["stage"].as_str().unwrap_or("")
            ),
        ));
    }
    Ok(i)
}

fn require_digest<'a>(
    payload: &'a Value,
    field: &str,
    event_type: &str,
) -> Result<&'a str, AppError> {
    let digest = payload.get(field).and_then(Value::as_str).ok_or_else(|| {
        AppError::integrity(
            "run_corrupt",
            format!("{event_type} has no string payload.{field}"),
        )
    })?;
    let valid = digest.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    });
    if !valid {
        return Err(AppError::integrity(
            "run_corrupt",
            format!("{event_type} has invalid payload.{field} '{digest}'"),
        ));
    }
    Ok(digest)
}

fn validate_stage_evidence(stage: &Value) -> Result<(), AppError> {
    if stage["receipts"].as_array().is_none_or(Vec::is_empty) {
        return Err(AppError::integrity(
            "run_corrupt",
            format!("stage '{}' passed without an accepted receipt", stage["id"]),
        ));
    }
    let outputs = stage
        .get("declared_outputs")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            AppError::integrity(
                "run_corrupt",
                format!(
                    "stage '{}' has no authoritative declared output list",
                    stage["id"]
                ),
            )
        })?;
    let recorded = stage["artifact_names"]
        .as_array()
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    for output in outputs {
        let Some(name) = output.as_str() else {
            return Err(AppError::integrity(
                "run_corrupt",
                "run.created declares a non-string stage output",
            ));
        };
        if !recorded.iter().any(|v| v.as_str() == Some(name)) {
            return Err(AppError::integrity(
                "run_corrupt",
                format!(
                    "stage '{}' never recorded declared output '{name}'",
                    stage["id"]
                ),
            ));
        }
    }
    Ok(())
}

fn strip_projection_metadata(manifest: &mut Value) {
    if let Some(stages) = manifest["stages"].as_array_mut() {
        for stage in stages {
            if let Some(obj) = stage.as_object_mut() {
                obj.remove("gate_required");
                obj.remove("declared_gate");
                obj.remove("declared_outputs");
                obj.remove("artifact_names");
            }
        }
    }
}

fn validate_gate_event(stage: &Value, payload: &Value, event_type: &str) -> Result<(), AppError> {
    let declared = stage
        .get("declared_gate")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            AppError::integrity(
                "run_corrupt",
                format!(
                    "{event_type} appears for stage '{}' with no declared gate",
                    stage["id"]
                ),
            )
        })?;
    let observed = payload
        .get("gate_id")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            AppError::integrity(
                "run_corrupt",
                format!("{event_type} has no string payload.gate_id"),
            )
        })?;
    if observed != declared {
        return Err(AppError::integrity(
            "run_corrupt",
            format!(
                "{event_type} names gate '{observed}', but stage '{}' declares '{declared}'",
                stage["id"]
            ),
        ));
    }
    Ok(())
}

fn strip_doctrine_for_manifest(d: &Value) -> Value {
    // The manifest schema allows pack/ref/resolved_sha256 only; the local
    // pack directory stays in the event payload for bundle assembly.
    let mut out = serde_json::Map::new();
    for k in ["pack", "ref", "resolved_sha256"] {
        if let Some(v) = d.get(k) {
            out.insert(k.to_string(), v.clone());
        }
    }
    Value::Object(out)
}

fn stage_index(manifest: &Value, event: &Value) -> Result<usize, AppError> {
    let stage_id = event["stage"]
        .as_str()
        .ok_or_else(|| AppError::integrity("run_corrupt", "stage event without stage field"))?;
    manifest["stages"]
        .as_array()
        .and_then(|stages| stages.iter().position(|s| s["id"] == stage_id))
        .ok_or_else(|| {
            AppError::integrity(
                "run_corrupt",
                format!("event references undeclared stage '{stage_id}'"),
            )
        })
}

fn set_stage_status(manifest: &mut Value, event: &Value, status: &str) -> Result<(), AppError> {
    let i = stage_index(manifest, event)?;
    manifest["stages"][i]["status"] = json!(status);
    Ok(())
}

fn set_stage_gate_record(
    manifest: &mut Value,
    event: &Value,
    payload: &Value,
) -> Result<(), AppError> {
    let i = stage_index(manifest, event)?;
    let digest = require_digest(
        payload,
        "record_sha256",
        event["type"].as_str().unwrap_or("stage gate event"),
    )?;
    manifest["stages"][i]["gate_record_sha256"] = json!(digest);
    Ok(())
}

fn push_stage_digest(
    manifest: &mut Value,
    event: &Value,
    field: &str,
    payload: &Value,
) -> Result<(), AppError> {
    let i = stage_index(manifest, event)?;
    let digest = require_digest(
        payload,
        "digest",
        event["type"].as_str().unwrap_or("stage event"),
    )?;
    manifest["stages"][i][field]
        .as_array_mut()
        .ok_or_else(|| {
            AppError::integrity(
                "run_corrupt",
                format!("stage field '{field}' is not an array"),
            )
        })?
        .push(json!(digest));
    if field == "artifacts" {
        let name = payload.get("name").and_then(Value::as_str).ok_or_else(|| {
            AppError::integrity(
                "run_corrupt",
                "stage.artifact_recorded has no string payload.name",
            )
        })?;
        if manifest["stages"][i].get("artifact_names").is_none() {
            manifest["stages"][i]["artifact_names"] = json!([]);
        }
        let names = manifest["stages"][i]["artifact_names"]
            .as_array_mut()
            .expect("projection artifact_names array");
        if names.iter().any(|v| v.as_str() == Some(name)) {
            return Err(AppError::integrity(
                "run_corrupt",
                format!(
                    "stage '{}' records artifact '{name}' more than once",
                    event["stage"]
                ),
            ));
        }
        names.push(json!(name));
    }
    Ok(())
}

/// Rebuild + atomically rewrite `manifest.json` from the event authority.
pub fn rewrite_manifest(run_dir: &Path) -> Result<Value, AppError> {
    let events = crate::store::read_events(run_dir)?;
    let manifest = project(&events)?;
    let schema_text = match manifest.get("schema").and_then(Value::as_str) {
        Some("stammtisch.manifest.v0") => crate::schemas::RUN_MANIFEST,
        Some("stammtisch.manifest.v1") => crate::schemas::RUN_MANIFEST_V1,
        other => {
            return Err(AppError::integrity(
                "run_corrupt",
                format!("projection produced unknown manifest revision {other:?}"),
            ))
        }
    };
    let schema: Value = serde_json::from_str(schema_text).expect("embedded schema parses");
    let errs = crate::jsonval::violations(&schema, &manifest);
    if !errs.is_empty() {
        return Err(AppError::internal(format!(
            "projection violates manifest schema: {}",
            errs.join("; ")
        )));
    }
    atomic_write(
        &run_dir.join("manifest.json"),
        canon::canonical_bytes(&manifest).as_slice(),
    )?;
    Ok(manifest)
}

/// Load the manifest projection. `events.jsonl` is the authority (§4), so
/// this always folds the event log — a corrupt log fails closed instead of
/// hiding behind a stale projection — and atomically rewrites
/// `manifest.json` as a side effect (the projection is rebuildable by
/// construction; conformance item 7).
pub fn load_manifest(run_dir: &Path) -> Result<Value, AppError> {
    rewrite_manifest(run_dir)
}

// ------------------------------------------------------------- reconcile

/// `stammtisch reconcile` (architecture doc P5): bind durable state and
/// report — never advance work. Rebuilds every projection from its event
/// log, marks interrupted runs with a durable `run.reconciled` audit event
/// (no stage is re-invoked), and clears the launch lock.
pub fn reconcile(root: &StateRoot) -> Result<Value, AppError> {
    let mut runs = Vec::new();
    let mut corrupt = Vec::new();
    for run_id in root.list_run_ids()? {
        let run_dir = root.run_dir(&run_id);
        match crate::store::read_events(&run_dir) {
            Ok(events) => {
                let last = events.last().expect("nonempty");
                let terminal = last["type"]
                    .as_str()
                    .map(|t| {
                        matches!(
                            t,
                            "run.completed"
                                | "run.blocked"
                                | "run.failed"
                                | "run.halted"
                                | "run.cancelled"
                        )
                    })
                    .unwrap_or(false);
                let mut interrupted = false;
                let mut remote_cancel: Vec<Value> = Vec::new();
                if !terminal {
                    interrupted = true;
                    // The local run died without reaching a terminal state;
                    // a remote A2A task it spawned may still be running and
                    // holding the product's one-active slot. Best-effort
                    // CancelTask (refusals are recorded, never fatal).
                    remote_cancel = cancel_remote_tasks(&run_dir);
                    let mut w = EventWriter::resume(&run_dir, &run_id, events.len() as u64);
                    w.emit(
                        "run.reconciled",
                        None,
                        json!({
                            "note": "interrupted run bound to durable state; no work advanced",
                            "last_seq": events.len(),
                        }),
                    )?;
                }
                let manifest = match rewrite_manifest(&run_dir) {
                    Ok(m) => m,
                    Err(e) => {
                        // One bad projection must not sink the whole
                        // reconcile; isolate it in the corrupt list.
                        corrupt.push(json!({"run_id": run_id, "error": e.message}));
                        continue;
                    }
                };
                runs.push(json!({
                    "run_id": run_id,
                    "state": manifest["state"]["code"],
                    "interrupted": interrupted,
                    "events": crate::store::read_events(&run_dir)?.len(),
                    "remote_cancel": remote_cancel,
                }));
            }
            Err(e) => {
                // Fail closed: report, do not guess at partial bytes.
                corrupt.push(json!({"run_id": run_id, "error": e.message}));
            }
        }
    }

    let lock_path = LaunchLock::lock_path(root);
    let (lock_present, lock_removed, lock_holder) = if lock_path.exists() {
        let holder = std::fs::read_to_string(&lock_path).unwrap_or_else(|_| "<unreadable>".into());
        std::fs::remove_file(&lock_path)?;
        (true, true, holder)
    } else {
        (false, false, String::new())
    };

    Ok(json!({
        "runs": runs,
        "corrupt": corrupt,
        "launch_lock": {
            "present": lock_present,
            "removed": lock_removed,
            "holder": lock_holder,
        }
    }))
}

/// Best-effort CancelTask for every distinct remote task an interrupted run
/// left behind. Refusals and transport failures are recorded as strings in
/// the reconcile report — recovery must never turn into a new failure mode.
fn cancel_remote_tasks(run_dir: &Path) -> Vec<Value> {
    let mut report: Vec<Value> = Vec::new();
    let Ok(pipeline_bytes) = std::fs::read(run_dir.join("pipeline.json")) else {
        return report;
    };
    let Ok(pipeline_value) = serde_json::from_slice::<Value>(&pipeline_bytes) else {
        return report;
    };
    let mut bindings: Vec<(String, String)> = Vec::new();
    if let Some(stages) = pipeline_value.get("stages").and_then(Value::as_array) {
        for stage in stages {
            let Some(runtime) = stage.get("runtime") else {
                continue;
            };
            if runtime.get("protocol").and_then(Value::as_str) != Some("a2a") {
                continue;
            }
            let Some(endpoint) = runtime.get("endpoint").and_then(Value::as_str) else {
                continue;
            };
            let token_env = runtime
                .get("token_env")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string();
            bindings.push((endpoint.to_string(), token_env));
        }
    }
    if bindings.is_empty() {
        return report;
    }
    let mut task_ids: Vec<String> = Vec::new();
    if let Ok(entries) = std::fs::read_dir(run_dir.join("receipts")) {
        for entry in entries.flatten() {
            let path = entry.path();
            let Ok(bytes) = std::fs::read(&path) else {
                continue;
            };
            let Ok(value) = serde_json::from_slice::<Value>(&bytes) else {
                continue;
            };
            if value.get("schema").and_then(Value::as_str) != Some("a2a.invocation.v2") {
                continue;
            }
            if let Some(task_id) = value.get("task_id").and_then(Value::as_str) {
                if !task_ids.iter().any(|t| t == task_id) {
                    task_ids.push(task_id.to_string());
                }
            }
        }
    }
    for task_id in task_ids {
        for (endpoint, token_env) in &bindings {
            let client = match crate::adapters::a2a::client::A2aClient::new(
                endpoint,
                if token_env.is_empty() {
                    None
                } else {
                    Some(token_env)
                },
            ) {
                Ok(c) => c,
                Err(e) => {
                    report.push(json!({"task_id": task_id, "error": format!("client setup: {e}")}));
                    continue;
                }
            };
            match client.cancel_task(&task_id) {
                Ok(_) => report.push(json!({"task_id": task_id, "cancelled": true})),
                Err(e) => report.push(json!({"task_id": task_id, "error": e.message})),
            }
        }
    }
    report
}

/// Append an externally-visible note line (used by tests and repair paths).
pub fn append_raw_line(run_dir: &Path, line: &str) -> Result<(), AppError> {
    append_line_fsync(&run_dir.join("events.jsonl"), line)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::StateRoot;

    #[test]
    fn cancel_remote_tasks_collects_task_ids_and_reports_failures() {
        let dir = std::env::temp_dir()
            .join(format!("stammtisch-cancel-{}", ids::uuid_v7().unwrap()));
        std::fs::create_dir_all(dir.join("receipts")).unwrap();
        // pipeline.json with one a2a stage
        std::fs::write(
            dir.join("pipeline.json"),
            serde_json::to_vec(&json!({
                "schema": "stammtisch.pipeline.v0",
                "id": "p",
                "doctrine": {"pack": "galahad"},
                "stages": [
                    {"id": "brief", "product": "doctrine"},
                    {"id": "review", "product": "quinte",
                     "runtime": {"protocol": "a2a",
                                 "endpoint": "http://127.0.0.1:1/",
                                 "token_env": "A2A_TOKEN"}}
                ]
            }))
            .unwrap(),
        )
        .unwrap();
        // two receipts, one with a task id (duplicated across receipts)
        std::fs::write(
            dir.join("receipts").join("review.0.json"),
            serde_json::to_vec(&json!({
                "schema": "a2a.invocation.v2",
                "task_id": "task-123"
            }))
            .unwrap(),
        )
        .unwrap();
        std::fs::write(
            dir.join("receipts").join("review.1.json"),
            serde_json::to_vec(&json!({
                "schema": "a2a.invocation.v2",
                "task_id": "task-123"
            }))
            .unwrap(),
        )
        .unwrap();
        std::fs::write(
            dir.join("receipts").join("review.2.json"),
            b"not json",
        )
        .unwrap();
        let report = cancel_remote_tasks(&dir);
        // one distinct task id reported exactly once; the endpoint is
        // unreachable, so the report carries the transport failure.
        let ids: Vec<&str> = report
            .iter()
            .filter_map(|entry| entry.get("task_id").and_then(Value::as_str))
            .collect();
        assert_eq!(ids, vec!["task-123"]);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn project_fold_basic() {
        let root = StateRoot {
            path: std::env::temp_dir()
                .join(format!("stammtisch-runner-{}", ids::uuid_v7().unwrap())),
        };
        root.init().unwrap();
        let run_dir = root.run_dir("019b4e5a-2c3d-7e8f-9a0b-1c2d3e4f5a6b");
        std::fs::create_dir_all(&run_dir).unwrap();
        let mut w = EventWriter::new(&run_dir, "019b4e5a-2c3d-7e8f-9a0b-1c2d3e4f5a6b");
        w.emit(
            "run.created",
            None,
            json!({
                "pipeline": {"id": "p", "canonical_sha256": format!("sha256:{}", "a".repeat(64))},
                "doctrine": {"pack": "galahad", "resolved_sha256": format!("sha256:{}", "b".repeat(64)), "dir": "/x"},
                "stages": [{"id": "brief", "product": "doctrine", "gate": Value::Null, "outputs": ["brief.json"]}],
                "state_root": "/root",
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
        let events = crate::store::read_events(&run_dir).unwrap();
        let m = project(&events).unwrap();
        assert_eq!(m["state"]["code"], "running");
        assert_eq!(m["state"]["current_stage"], "brief");
        assert_eq!(m["stages"][0]["status"], "running");
        // doctrine dir stays out of the manifest (schema-additionalProperties)
        assert!(m["doctrine"].get("dir").is_none());
        std::fs::remove_dir_all(&root.path).ok();
    }

    #[test]
    fn project_rejects_unknown_event_type() {
        let root = StateRoot {
            path: std::env::temp_dir()
                .join(format!("stammtisch-runner-{}", ids::uuid_v7().unwrap())),
        };
        root.init().unwrap();
        let run_dir = root.run_dir("r1");
        std::fs::create_dir_all(&run_dir).unwrap();
        // Bypass schema-checked emit: craft an event with a valid-looking but
        // unknown type via raw append is blocked by schema enum, so simulate
        // by folding a synthesized in-memory event list.
        let events = vec![json!({
            "schema": crate::store::EVENT_SCHEMA, "run_id": "r1", "seq": 1,
            "type": "run.teleported", "at": "2026-08-09T00:00:00.000Z", "payload": {}
        })];
        let e = project(&events).unwrap_err();
        assert_eq!(e.code, "run_corrupt");
        std::fs::remove_dir_all(&root.path).ok();
    }

    fn event(seq: u64, event_type: &str, stage: Option<&str>, payload: Value) -> Value {
        let mut value = json!({
            "schema": crate::store::EVENT_SCHEMA,
            "run_id": "r1",
            "seq": seq,
            "type": event_type,
            "at": "2026-08-09T00:00:00.000Z",
            "payload": payload,
        });
        if let Some(stage) = stage {
            value["stage"] = json!(stage);
        }
        value
    }

    fn created(stages: Value) -> Value {
        event(
            1,
            "run.created",
            None,
            json!({
                "pipeline": {"id": "p", "canonical_sha256": format!("sha256:{}", "a".repeat(64))},
                "doctrine": {"pack": "galahad", "resolved_sha256": format!("sha256:{}", "b".repeat(64))},
                "stages": stages,
                "state_root": "/root",
            }),
        )
    }

    #[test]
    fn stage_inputs_are_exact_and_fail_closed() {
        let run_dir =
            std::env::temp_dir().join(format!("stammtisch-input-bind-{}", ids::uuid_v7().unwrap()));
        std::fs::create_dir_all(run_dir.join("artifacts")).unwrap();
        let wanted_bytes = br#"{"wanted":true}"#;
        let extra_bytes = br#"{"secret":true}"#;
        let wanted = canon::sha256_prefixed(wanted_bytes);
        let extra = canon::sha256_prefixed(extra_bytes);
        std::fs::write(
            run_dir
                .join("artifacts")
                .join(wanted.strip_prefix("sha256:").unwrap()),
            wanted_bytes,
        )
        .unwrap();
        std::fs::write(
            run_dir
                .join("artifacts")
                .join(extra.strip_prefix("sha256:").unwrap()),
            extra_bytes,
        )
        .unwrap();
        let stage = pipeline::validate(
            &json!({
                "schema": "stammtisch.pipeline.v0",
                "id": "input-bind",
                "doctrine": {"pack": "galahad"},
                "stages": [
                    {"id": "source", "product": "doctrine", "out": ["wanted.json", "secret.json"]},
                    {"id": "sink", "product": "highball", "adapter": "fake", "in": ["wanted.json"]}
                ]
            }),
            Path::new("pipeline.json"),
        )
        .unwrap()
        .stages
        .pop()
        .unwrap();
        let produced: BTreeMap<String, String> = [
            ("wanted.json".to_string(), wanted),
            ("secret.json".to_string(), extra),
        ]
        .into_iter()
        .collect();

        let bound = bind_stage_inputs(&stage, &produced, &run_dir).unwrap();
        assert_eq!(bound.len(), 1);
        assert!(bound.contains_key("wanted.json"));
        assert!(!bound.contains_key("secret.json"));

        let missing = BTreeMap::new();
        let error = bind_stage_inputs(&stage, &missing, &run_dir).unwrap_err();
        assert_eq!(error.code, "stage_input_missing");
        std::fs::remove_dir_all(run_dir).ok();
    }

    #[test]
    fn project_rejects_unknown_stage_instead_of_skipping_it() {
        let events = vec![
            created(
                json!([{"id": "brief", "product": "doctrine", "gate": Value::Null, "outputs": []}]),
            ),
            event(2, "run.staged", None, json!({})),
            event(3, "stage.started", Some("ghost"), json!({})),
        ];
        let error = project(&events).unwrap_err();
        assert_eq!(error.code, "run_corrupt");
        assert!(error.message.contains("undeclared stage 'ghost'"));
    }

    #[test]
    fn project_rejects_incomplete_completed_sequence() {
        let events = vec![
            created(json!([{
                "id": "brief", "product": "doctrine", "gate": Value::Null,
                "outputs": ["brief.json"]
            }])),
            event(2, "run.staged", None, json!({})),
            event(3, "stage.started", Some("brief"), json!({})),
            event(4, "run.gating", None, json!({})),
            event(
                5,
                "run.completed",
                None,
                json!({"bundle_manifest_sha256": format!("sha256:{}", "c".repeat(64))}),
            ),
        ];
        let error = project(&events).unwrap_err();
        assert_eq!(error.code, "run_corrupt");
        assert!(error.message.contains("passed without an accepted receipt"));

        let events = vec![
            created(
                json!([{"id": "brief", "product": "doctrine", "gate": Value::Null, "outputs": []}]),
            ),
            event(2, "run.staged", None, json!({})),
            event(3, "stage.started", Some("brief"), json!({})),
            event(
                4,
                "stage.receipt_accepted",
                Some("brief"),
                json!({"digest": format!("sha256:{}", "d".repeat(64))}),
            ),
            event(5, "run.gating", None, json!({})),
            event(6, "run.completed", None, json!({})),
        ];
        let error = project(&events).unwrap_err();
        assert_eq!(error.code, "run_corrupt");
        assert!(error.message.contains("bundle_manifest_sha256"));
    }

    #[test]
    fn project_keeps_legacy_v0_completed_logs_readable() {
        let receipt = format!("sha256:{}", "d".repeat(64));
        let artifact = format!("sha256:{}", "e".repeat(64));
        let events = vec![
            // Original run-event.v0 carried only id/product in run.created.
            created(json!([{"id": "brief", "product": "doctrine"}])),
            event(2, "run.staged", None, json!({})),
            event(3, "stage.started", Some("brief"), json!({})),
            event(
                4,
                "stage.receipt_accepted",
                Some("brief"),
                json!({"digest": receipt}),
            ),
            event(
                5,
                "stage.artifact_recorded",
                Some("brief"),
                json!({"name": "brief.json", "digest": artifact}),
            ),
            event(6, "run.gating", None, json!({})),
            // Original run.completed did not bind bundle/MANIFEST.json.
            event(7, "run.completed", None, json!({})),
        ];
        let manifest = project(&events).unwrap();
        assert_eq!(manifest["state"]["code"], "completed");
        assert!(manifest.get("bundle_manifest_sha256").is_none());
    }
}
