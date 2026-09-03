//! CLI surface (architecture doc §8): hand-rolled subcommand parsing,
//! `--json` envelopes on stdout, human text on stderr, exit codes
//! 0 / 1 / 2 / 3.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};

use crate::envelope;
use crate::error::AppError;
use crate::store::StateRoot;

#[derive(Debug)]
pub enum Command {
    Init,
    Validate { pipeline: PathBuf },
    Run { pipeline: PathBuf },
    Status { run_id: Option<String> },
    Inspect { run_id: String },
    Reconcile,
    Cancel { run_id: Option<String>, abandoned: bool },
    Export { run_id: String, out: PathBuf },
    Verify {
        bundle: PathBuf,
        signature: Option<PathBuf>,
        public_key: Option<PathBuf>,
    },
    Delete { run_id: String, force: bool },
    Version,
}

pub struct Cli {
    pub json: bool,
    pub command: Command,
}

const USAGE: &str = "stammtisch — deterministic pipelines with offline-verifiable deliverables

USAGE:
  stammtisch init [--json]
  stammtisch validate --pipeline FILE [--json]
  stammtisch run --pipeline FILE [--json]
  stammtisch status [RUN_ID] [--json]
  stammtisch inspect RUN_ID [--json]
  stammtisch reconcile [--json]
  stammtisch cancel RUN_ID [--json]
  stammtisch cancel --abandoned [--json]
  stammtisch export RUN_ID --out DIR [--json]
  stammtisch verify --bundle DIR [--signature FILE] [--public-key FILE] [--json]
  stammtisch delete RUN_ID [--force] [--json]

EXIT CODES: 0 completed/clean · 1 product-failure · 2 blocked/halted/integrity · 3 usage";

pub fn parse_args(args: &[String]) -> Result<Cli, AppError> {
    let mut json = false;
    let mut force = false;
    let mut abandoned = false;
    let mut positional: Vec<String> = Vec::new();
    let mut pipeline: Option<PathBuf> = None;
    let mut out: Option<PathBuf> = None;
    let mut bundle: Option<PathBuf> = None;
    let mut signature: Option<PathBuf> = None;
    let mut public_key: Option<PathBuf> = None;

    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--version" => {
                return Ok(Cli {
                    json: false,
                    command: Command::Version,
                })
            }
            "--json" => json = true,
            "--force" => force = true,
            "--abandoned" => abandoned = true,
            "--pipeline" => {
                i += 1;
                pipeline = Some(PathBuf::from(take_value(args, i, "--pipeline")?));
            }
            "--out" => {
                i += 1;
                out = Some(PathBuf::from(take_value(args, i, "--out")?));
            }
            "--bundle" => {
                i += 1;
                bundle = Some(PathBuf::from(take_value(args, i, "--bundle")?));
            }
            "--signature" => {
                i += 1;
                signature = Some(PathBuf::from(take_value(args, i, "--signature")?));
            }
            "--public-key" => {
                i += 1;
                public_key = Some(PathBuf::from(take_value(args, i, "--public-key")?));
            }
            other if other.starts_with("--") => {
                return Err(AppError::usage(
                    "args_invalid",
                    format!("unknown flag '{other}'"),
                ));
            }
            other => positional.push(other.to_string()),
        }
        i += 1;
    }

    let sub = positional
        .first()
        .cloned()
        .ok_or_else(|| AppError::usage("args_invalid", "missing subcommand"))?;
    let rest = &positional[1..];
    let command = match sub.as_str() {
        "init" => Command::Init,
        "validate" => Command::Validate {
            pipeline: pipeline.ok_or_else(|| missing("--pipeline"))?,
        },
        "run" => Command::Run {
            pipeline: pipeline.ok_or_else(|| missing("--pipeline"))?,
        },
        "status" => Command::Status {
            run_id: rest.first().cloned(),
        },
        "inspect" => Command::Inspect {
            run_id: rest
                .first()
                .cloned()
                .ok_or_else(|| AppError::usage("args_invalid", "inspect requires RUN_ID"))?,
        },
        "reconcile" => Command::Reconcile,
        "cancel" => {
            if rest.first().is_none() && !abandoned {
                return Err(AppError::usage(
                    "args_invalid",
                    "cancel requires RUN_ID or --abandoned",
                ));
            }
            Command::Cancel {
                run_id: rest.first().cloned(),
                abandoned,
            }
        }
        "export" => Command::Export {
            run_id: rest
                .first()
                .cloned()
                .ok_or_else(|| AppError::usage("args_invalid", "export requires RUN_ID"))?,
            out: out.ok_or_else(|| missing("--out"))?,
        },
        "verify" => {
            if public_key.is_some() && signature.is_none() {
                return Err(AppError::usage(
                    "args_invalid",
                    "--public-key requires --signature",
                ));
            }
            Command::Verify {
                bundle: bundle.ok_or_else(|| missing("--bundle"))?,
                signature,
                public_key,
            }
        }
        "delete" => Command::Delete {
            run_id: rest
                .first()
                .cloned()
                .ok_or_else(|| AppError::usage("args_invalid", "delete requires RUN_ID"))?,
            force,
        },
        other => {
            return Err(AppError::usage(
                "args_invalid",
                format!("unknown subcommand '{other}'"),
            ))
        }
    };
    Ok(Cli { json, command })
}

fn take_value(args: &[String], i: usize, flag: &str) -> Result<String, AppError> {
    args.get(i)
        .cloned()
        .ok_or_else(|| AppError::usage("args_invalid", format!("{flag} requires a value")))
}

fn missing(flag: &str) -> AppError {
    AppError::usage("args_invalid", format!("missing required {flag}"))
}

pub fn command_name(c: &Command) -> &'static str {
    match c {
        Command::Init => "init",
        Command::Validate { .. } => "validate",
        Command::Run { .. } => "run",
        Command::Status { .. } => "status",
        Command::Inspect { .. } => "inspect",
        Command::Reconcile => "reconcile",
        Command::Cancel { .. } => "cancel",
        Command::Export { .. } => "export",
        Command::Verify { .. } => "verify",
        Command::Delete { .. } => "delete",
        Command::Version => "version",
    }
}

/// Execute one command: prints the envelope when `--json`, human text on
/// stderr always, and returns the process exit code.
pub fn dispatch(cli: &Cli) -> i32 {
    if matches!(cli.command, Command::Version) {
        println!("stammtisch-core {}", env!("CARGO_PKG_VERSION"));
        return 0;
    }
    let name = command_name(&cli.command);
    match execute(&cli.command) {
        Ok((code, data, human)) => {
            envelope::note(human);
            if cli.json {
                envelope::print(&envelope::ok(name, data));
            }
            code
        }
        Err(e) => {
            envelope::note(format!("error: {e}"));
            if cli.json {
                envelope::print(&envelope::err(name, &e));
            } else if e.code == "args_invalid" {
                eprintln!("{USAGE}");
            }
            e.exit_code()
        }
    }
}

/// (exit_code, envelope data, human summary)
fn execute(command: &Command) -> Result<(i32, Value, String), AppError> {
    match command {
        Command::Init => {
            let root = StateRoot::resolve()?;
            root.init()?;
            let data = json!({"state_root": root.path.display().to_string()});
            Ok((
                0,
                data.clone(),
                format!("initialized {}", data["state_root"]),
            ))
        }
        Command::Validate { pipeline } => {
            let root = StateRoot::resolve()?;
            let spec = crate::pipeline::load(pipeline)?;
            let doctrine = crate::doctrine::resolve(&spec, &root.path)?;
            crate::runner::preflight_gates(&spec, &doctrine)?;
            let gates: Vec<&str> = spec
                .stages
                .iter()
                .filter_map(|s| s.gate.as_deref())
                .collect();
            let data = json!({
                "pipeline_id": spec.id,
                "canonical_sha256": spec.canonical_sha256,
                "doctrine": {"pack": doctrine.name, "resolved_sha256": doctrine.digest},
                "stages": spec.stages.len(),
                "gates": gates,
            });
            Ok((
                0,
                data,
                format!(
                    "pipeline '{}' valid ({} stages, doctrine {} {})",
                    spec.id,
                    spec.stages.len(),
                    doctrine.name,
                    doctrine.digest
                ),
            ))
        }
        Command::Run { pipeline } => {
            let root = StateRoot::resolve()?;
            let report = crate::runner::run_pipeline(&root, Path::new(pipeline))?;
            let data = json!({
                "run_id": report.run_id,
                "pipeline_id": report.pipeline_id,
                "terminal": report.terminal.as_str(),
                "bundle_manifest_sha256": report.bundle_manifest_sha256,
                "detail": report.detail,
            });
            let human = match report.terminal {
                crate::runner::Terminal::Completed => {
                    format!(
                        "run {} completed — bundle {}",
                        report.run_id,
                        report.bundle_manifest_sha256.clone().unwrap_or_default()
                    )
                }
                other => format!(
                    "run {} {} — {}",
                    report.run_id,
                    other.as_str(),
                    report.detail
                ),
            };
            Ok((report.terminal.exit_code(), data, human))
        }
        Command::Status { run_id } => {
            let root = require_root()?;
            match run_id {
                None => {
                    let mut runs = Vec::new();
                    for id in root.list_run_ids()? {
                        match crate::runner::load_manifest(&root.run_dir(&id)) {
                            Ok(manifest) => runs.push(json!({
                                "run_id": id,
                                "pipeline_id": manifest["pipeline"]["id"],
                                "state": manifest["state"]["code"],
                                "created_at": manifest["created_at"],
                            })),
                            Err(error) => runs.push(json!({
                                "run_id": id,
                                "pipeline_id": Value::Null,
                                "state": "corrupt",
                                "error": error.message,
                            })),
                        }
                    }
                    let data = json!({"state_root": root.path.display().to_string(), "runs": runs});
                    let human = format!("{} run(s) in {}", runs_len(&data), root.path.display());
                    Ok((0, data, human))
                }
                Some(id) => {
                    let run_dir = root.run_dir(id);
                    if !run_dir.is_dir() {
                        return Err(AppError::usage(
                            "run_unknown",
                            format!("no run with id '{id}' in this state root"),
                        ));
                    }
                    let manifest = crate::runner::load_manifest(&run_dir)?;
                    let human = format!(
                        "run {} — {} ({})",
                        id,
                        manifest["state"]["code"].as_str().unwrap_or("?"),
                        manifest["pipeline"]["id"].as_str().unwrap_or("?")
                    );
                    Ok((0, manifest, human))
                }
            }
        }
        Command::Inspect { run_id } => {
            let root = require_root()?;
            let run_dir = root.run_dir(run_id);
            if !run_dir.is_dir() {
                return Err(AppError::usage(
                    "run_unknown",
                    format!("no run with id '{run_id}' in this state root"),
                ));
            }
            let manifest = crate::runner::load_manifest(&run_dir)?;
            let events = crate::store::read_events(&run_dir)?;
            let mut receipts = Vec::new();
            let mut gates = Vec::new();
            let mut artifacts = Vec::new();
            // Evidence files are stage-addressed, not merely digest-addressed.
            // Two stages may legitimately emit identical canonical receipts;
            // accepting files through one global digest set would both reject
            // that case and allow a receipt to be moved under the wrong stage.
            let mut expected_receipts = std::collections::BTreeMap::new();
            let mut expected_gates = std::collections::BTreeMap::new();
            let mut expected_artifacts: std::collections::BTreeMap<
                String,
                Vec<(String, String, String)>,
            > = std::collections::BTreeMap::new();
            for stage in manifest["stages"].as_array().ok_or_else(|| {
                AppError::integrity("run_corrupt", "projected manifest has no stages array")
            })? {
                let stage_id = stage["id"].as_str().ok_or_else(|| {
                    AppError::integrity("run_corrupt", "projected stage has no string id")
                })?;
                let stage_receipts = stage["receipts"].as_array().ok_or_else(|| {
                    AppError::integrity(
                        "run_corrupt",
                        format!("projected stage '{stage_id}' has no receipts array"),
                    )
                })?;
                for (index, digest) in stage_receipts.iter().enumerate() {
                    let digest = digest.as_str().ok_or_else(|| {
                        AppError::integrity(
                            "run_corrupt",
                            format!("projected stage '{stage_id}' has a non-string receipt digest"),
                        )
                    })?;
                    expected_receipts.insert(
                        format!("{stage_id}.{index}.json"),
                        (stage_id.to_string(), digest.to_string()),
                    );
                }
                for event in events.iter().filter(|event| {
                    event.get("stage").and_then(Value::as_str) == Some(stage_id)
                        && event.get("type").and_then(Value::as_str)
                            == Some("stage.artifact_recorded")
                }) {
                    let name = event["payload"]
                        .get("name")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            AppError::integrity(
                                "run_corrupt",
                                format!("stage '{stage_id}' artifact event has no string name"),
                            )
                        })?;
                    let digest = event["payload"]
                        .get("digest")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            AppError::integrity(
                                "run_corrupt",
                                format!("stage '{stage_id}' artifact event has no string digest"),
                            )
                        })?;
                    let hex = digest.strip_prefix("sha256:").ok_or_else(|| {
                        AppError::integrity(
                            "run_corrupt",
                            format!("stage '{stage_id}' artifact digest is malformed"),
                        )
                    })?;
                    expected_artifacts
                        .entry(hex.to_string())
                        .or_default()
                        .push((stage_id.to_string(), name.to_string(), digest.to_string()));
                }
                if let Some(digest) = stage.get("gate_record_sha256").and_then(Value::as_str) {
                    let matching_events: Vec<&Value> = events
                        .iter()
                        .filter(|event| {
                            event.get("stage").and_then(Value::as_str) == Some(stage_id)
                                && matches!(
                                    event.get("type").and_then(Value::as_str),
                                    Some("stage.gate_passed" | "stage.gate_failed")
                                )
                                && event["payload"]
                                    .get("record_sha256")
                                    .and_then(Value::as_str)
                                    == Some(digest)
                        })
                        .collect();
                    if matching_events.len() != 1 {
                        return Err(AppError::integrity(
                            "run_corrupt",
                            format!(
                                "stage '{stage_id}' gate digest {digest} has {} matching events",
                                matching_events.len()
                            ),
                        ));
                    }
                    let event = matching_events[0];
                    let gate_id = event["payload"]
                        .get("gate_id")
                        .and_then(Value::as_str)
                        .ok_or_else(|| {
                            AppError::integrity(
                                "run_corrupt",
                                format!("stage '{stage_id}' gate event has no string gate_id"),
                            )
                        })?;
                    let decision = if event["type"] == "stage.gate_passed" {
                        "pass"
                    } else {
                        "fail"
                    };
                    expected_gates.insert(
                        format!("{stage_id}.gate.json"),
                        (
                            stage_id.to_string(),
                            digest.to_string(),
                            gate_id.to_string(),
                            decision.to_string(),
                        ),
                    );
                }
            }
            let mut observed_receipts = std::collections::BTreeSet::new();
            let mut observed_gates = std::collections::BTreeSet::new();
            let mut observed_artifacts = std::collections::BTreeSet::new();
            let mut artifact_entries =
                std::fs::read_dir(run_dir.join("artifacts"))?.collect::<Result<Vec<_>, _>>()?;
            artifact_entries.sort_by_key(|entry| entry.file_name());
            for entry in artifact_entries {
                let path = entry.path();
                if !std::fs::symlink_metadata(&path)?.file_type().is_file() {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!("{}: artifact entry is not a file", path.display()),
                    ));
                }
                let file = path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .ok_or_else(|| {
                        AppError::integrity(
                            "run_corrupt",
                            format!("{}: artifact filename is not UTF-8", path.display()),
                        )
                    })?;
                let bindings = expected_artifacts.get(file).ok_or_else(|| {
                    AppError::integrity(
                        "run_corrupt",
                        format!("{}: artifact is not recorded by events", path.display()),
                    )
                })?;
                let bytes = std::fs::read(&path)?;
                serde_json::from_slice::<Value>(&bytes).map_err(|error| {
                    AppError::integrity(
                        "run_corrupt",
                        format!("{}: artifact is not JSON ({error})", path.display()),
                    )
                })?;
                let digest = crate::canon::sha256_prefixed(&bytes);
                if bindings.iter().any(|binding| binding.2 != digest)
                    || file != digest.trim_start_matches("sha256:")
                {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!(
                            "{}: artifact digest {digest} does not match every event binding",
                            path.display()
                        ),
                    ));
                }
                observed_artifacts.insert(file.to_string());
                let binding_values: Vec<Value> = bindings
                    .iter()
                    .map(|(stage, name, _)| json!({"stage": stage, "name": name}))
                    .collect();
                artifacts.push(json!({
                    "file": file,
                    "sha256": digest,
                    "bindings": binding_values,
                }));
            }
            let mut receipt_entries =
                std::fs::read_dir(run_dir.join("receipts"))?.collect::<Result<Vec<_>, _>>()?;
            receipt_entries.sort_by_key(|entry| entry.file_name());
            for entry in receipt_entries {
                let path = entry.path();
                if !std::fs::symlink_metadata(&path)?.file_type().is_file() {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!("{}: receipt entry is not a file", path.display()),
                    ));
                }
                let file = path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .ok_or_else(|| {
                        AppError::integrity(
                            "run_corrupt",
                            format!("{}: receipt filename is not UTF-8", path.display()),
                        )
                    })?;
                let (expected_stage, expected_digest) =
                    expected_receipts.get(file).ok_or_else(|| {
                        AppError::integrity(
                            "run_corrupt",
                            format!("{}: unexpected receipt filename", path.display()),
                        )
                    })?;
                let bytes = std::fs::read(&path)?;
                let (_, parsed) = crate::contracts::validate_receipt(&bytes).map_err(|e| {
                    AppError::integrity(
                        "run_corrupt",
                        format!("{}: invalid receipt ({e})", path.display()),
                    )
                })?;
                let digest = crate::canon::sha256_prefixed(&bytes);
                if digest != *expected_digest {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!(
                            "{}: receipt digest {digest} does not match event-recorded {expected_digest}",
                            path.display()
                        ),
                    ));
                }
                if let Some(receipt_stage) = parsed.get("stage").and_then(Value::as_str) {
                    if receipt_stage != expected_stage {
                        return Err(AppError::integrity(
                            "run_corrupt",
                            format!(
                                "{}: receipt names stage '{receipt_stage}', expected '{expected_stage}'",
                                path.display()
                            ),
                        ));
                    }
                }
                if !observed_receipts.insert(file.to_string()) {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!("{}: duplicate receipt filename", path.display()),
                    ));
                }
                receipts.push(json!({
                    "file": file,
                    "stage": expected_stage,
                    "sha256": digest,
                    "receipt": parsed,
                }));
            }
            let mut gate_entries =
                std::fs::read_dir(run_dir.join("gates"))?.collect::<Result<Vec<_>, _>>()?;
            gate_entries.sort_by_key(|entry| entry.file_name());
            for entry in gate_entries {
                let path = entry.path();
                if !std::fs::symlink_metadata(&path)?.file_type().is_file() {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!("{}: gate entry is not a file", path.display()),
                    ));
                }
                let file = path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .ok_or_else(|| {
                        AppError::integrity(
                            "run_corrupt",
                            format!("{}: gate filename is not UTF-8", path.display()),
                        )
                    })?;
                let (expected_stage, expected_digest, expected_gate, expected_decision) =
                    expected_gates.get(file).ok_or_else(|| {
                        AppError::integrity(
                            "run_corrupt",
                            format!("{}: unexpected gate filename", path.display()),
                        )
                    })?;
                let bytes = std::fs::read(&path)?;
                let parsed: Value = serde_json::from_slice(&bytes).map_err(|e| {
                    AppError::integrity(
                        "run_corrupt",
                        format!("{}: unparseable gate record ({e})", path.display()),
                    )
                })?;
                let schema: Value = serde_json::from_str(crate::schemas::GATE_RECORD)
                    .expect("embedded schema parses");
                let violations = crate::jsonval::violations(&schema, &parsed);
                if !violations.is_empty() {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!(
                            "{}: invalid gate record ({})",
                            path.display(),
                            violations.join("; ")
                        ),
                    ));
                }
                let digest = crate::canon::sha256_prefixed(&bytes);
                if digest != *expected_digest {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!(
                            "{}: gate record digest {digest} does not match event-recorded {expected_digest}",
                            path.display()
                        ),
                    ));
                }
                for (field, expected) in [
                    ("run_id", run_id.as_str()),
                    ("stage", expected_stage.as_str()),
                    ("gate_id", expected_gate.as_str()),
                    ("decision", expected_decision.as_str()),
                ] {
                    if parsed.get(field).and_then(Value::as_str) != Some(expected) {
                        return Err(AppError::integrity(
                            "run_corrupt",
                            format!(
                                "{}: gate record {field} does not match authoritative event binding '{expected}'",
                                path.display()
                            ),
                        ));
                    }
                }
                if !observed_gates.insert(file.to_string()) {
                    return Err(AppError::integrity(
                        "run_corrupt",
                        format!("{}: duplicate gate filename", path.display()),
                    ));
                }
                gates.push(json!({
                    "file": file,
                    "stage": expected_stage,
                    "sha256": digest,
                    "record": parsed,
                }));
            }
            let expected_receipt_files = expected_receipts.keys().cloned().collect();
            if observed_receipts != expected_receipt_files {
                let missing: Vec<_> = expected_receipt_files
                    .difference(&observed_receipts)
                    .cloned()
                    .collect();
                return Err(AppError::integrity(
                    "run_corrupt",
                    format!("event-recorded receipt files are missing: {missing:?}"),
                ));
            }
            let expected_gate_files = expected_gates.keys().cloned().collect();
            if observed_gates != expected_gate_files {
                let missing: Vec<_> = expected_gate_files
                    .difference(&observed_gates)
                    .cloned()
                    .collect();
                return Err(AppError::integrity(
                    "run_corrupt",
                    format!("event-recorded gate files are missing: {missing:?}"),
                ));
            }
            let expected_artifact_files = expected_artifacts.keys().cloned().collect();
            if observed_artifacts != expected_artifact_files {
                let missing: Vec<_> = expected_artifact_files
                    .difference(&observed_artifacts)
                    .cloned()
                    .collect();
                return Err(AppError::integrity(
                    "run_corrupt",
                    format!("event-recorded artifact files are missing: {missing:?}"),
                ));
            }
            let data = json!({
                "manifest": manifest,
                "events": events,
                "artifacts": artifacts,
                "receipts": receipts,
                "gates": gates,
            });
            let human = format!(
                "run {} — {} artifact(s), {} receipt(s), {} gate record(s)",
                run_id,
                artifacts.len(),
                receipts.len(),
                gates.len()
            );
            Ok((0, data, human))
        }
        Command::Cancel { run_id, abandoned } => {
            let root = require_root()?;
            let data = if *abandoned && run_id.is_none() {
                crate::runner::cancel_abandoned(&root)?
            } else if let Some(run_id) = run_id {
                crate::runner::cancel_run(&root, run_id)?
            } else {
                crate::runner::cancel_abandoned(&root)?
            };
            let sealed = data
                .get("sealed")
                .and_then(Value::as_array)
                .map(Vec::len)
                .or_else(|| data.get("sealed").and_then(Value::as_bool).map(|b| usize::from(b)))
                .unwrap_or(0);
            let human = if *abandoned && run_id.is_none() {
                format!("cancelled {sealed} abandoned run(s)")
            } else {
                format!("cancelled run {}", run_id.as_deref().unwrap_or("?"))
            };
            Ok((0, data, human))
        }
        Command::Reconcile => {
            let root = StateRoot::resolve()?;
            root.init()?;
            let report = crate::runner::reconcile(&root)?;
            let corrupt = report["corrupt"].as_array().map(Vec::len).unwrap_or(0);
            let bound = report["runs"].as_array().map(Vec::len).unwrap_or(0);
            let lock_note = if report["launch_lock"]["removed"].as_bool().unwrap_or(false) {
                "removed (stale)"
            } else if report["launch_lock"]["live"].as_bool().unwrap_or(false) {
                "still held (live holder)"
            } else {
                "not held"
            };
            let human = format!(
                "reconciled {} run(s), {} corrupt, launch lock {}",
                bound, corrupt, lock_note
            );
            // Corrupt run dirs are reported, not fatal: the launch lock is
            // already released and every readable run is bound above. A
            // half-written events log (SIGKILL mid-append) must not turn
            // recovery into a failure — the operator disposes of corrupt
            // dirs from the report.
            let _ = corrupt;
            Ok((0, report, human))
        }
        Command::Export { run_id, out } => {
            let root = require_root()?;
            let data = crate::bundle::export(&root, run_id, Path::new(out))?;
            let human = format!(
                "exported run {} to {} (MANIFEST {})",
                run_id,
                out.display(),
                data["manifest_sha256"].as_str().unwrap_or("")
            );
            Ok((0, data, human))
        }
        Command::Verify {
            bundle,
            signature,
            public_key,
        } => {
            let (pass, report) = crate::bundle::verify(Path::new(bundle))?;
            let mut report = report;
            // The signature hook is optional (P3): it runs only when a
            // signature file was passed, only after bundle verification
            // itself passed (a broken bundle fails closed regardless),
            // and it is fail-closed — an absent minisign or a rejected
            // signature is an error, never a silent skip.
            if pass {
                if let Some(sig) = signature {
                    crate::bundle::verify_signature(
                        Path::new(bundle),
                        sig,
                        public_key.as_deref(),
                    )?;
                    report["signature_verified"] = json!(true);
                }
            }
            let failures = report["failures"].as_array().map(Vec::len).unwrap_or(0);
            let human = if pass {
                format!(
                    "bundle {} verified: {} entries, {} receipts, {} gate records",
                    bundle.display(),
                    report["entries"],
                    report["receipts"],
                    report["gate_records"]
                )
            } else {
                format!(
                    "bundle {} FAILED verification: {failures} failure(s)",
                    bundle.display()
                )
            };
            Ok((if pass { 0 } else { 2 }, report, human))
        }
        Command::Delete { run_id, force } => {
            let root = require_root()?;
            let run_dir = root.run_dir(run_id);
            if !run_dir.is_dir() {
                return Err(AppError::usage(
                    "run_unknown",
                    format!("no run with id '{run_id}' in this state root"),
                ));
            }
            let manifest = crate::runner::load_manifest(&run_dir);
            let (state, terminal) = match manifest {
                Ok(manifest) => {
                    let state = manifest["state"]["code"]
                        .as_str()
                        .unwrap_or("?")
                        .to_string();
                    let terminal = matches!(
                        state.as_str(),
                        "completed" | "blocked" | "failed" | "halted" | "cancelled"
                    );
                    (state, terminal)
                }
                Err(error) => {
                    // A corrupt run dir (events-first: the log is the
                    // authority) cannot prove a terminal state. Only
                    // --force may dispose of it — reconcile's report tells
                    // the operator to do exactly that; without --force the
                    // corruption stays fail-closed, never guessed away.
                    if !*force {
                        return Err(error);
                    }
                    ("corrupt".to_string(), true)
                }
            };
            if !terminal && !*force {
                return Err(AppError::usage(
                    "run_active",
                    format!(
                        "run '{run_id}' is '{state}'; refusing to delete a \
                         non-terminal run (use --force to override)"
                    ),
                ));
            }
            std::fs::remove_dir_all(&run_dir)?;
            if *force {
                // A forced delete of a live run must not leave a stale
                // launch lock pointing at the removed run behind.
                let lock_path = crate::store::LaunchLock::lock_path(&root);
                if let Ok(held) = std::fs::read_to_string(&lock_path) {
                    if held.contains(&format!("\"run_id\":\"{run_id}\"")) {
                        let _ = std::fs::remove_file(&lock_path);
                    }
                }
            }
            let data = json!({"run_id": run_id, "state": state, "removed": true});
            Ok((0, data, format!("deleted run {run_id}")))
        }
        Command::Version => Ok((
            0,
            json!({ "version": env!("CARGO_PKG_VERSION") }),
            format!("stammtisch-core {}", env!("CARGO_PKG_VERSION")),
        )),
    }
}

fn require_root() -> Result<StateRoot, AppError> {
    let root = StateRoot::resolve()?;
    if !root.is_initialized() {
        return Err(AppError::usage(
            "state_root_uninitialized",
            format!(
                "state root {} is not initialized; run `stammtisch init` first",
                root.path.display()
            ),
        ));
    }
    Ok(root)
}

fn runs_len(data: &Value) -> usize {
    data["runs"].as_array().map(Vec::len).unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_flag_parses_and_prints_identity() {
        let cli = parse_args(&["--version".to_string()]).expect("--version parses");
        assert!(matches!(cli.command, Command::Version));
        assert_eq!(dispatch(&cli), 0);
        assert_eq!(command_name(&cli.command), "version");
    }
}
