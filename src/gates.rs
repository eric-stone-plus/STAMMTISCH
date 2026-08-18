//! Gate evaluation (architecture doc §7): deterministic code over parsed
//! artifacts and receipts — never model judgment. Kinds:
//! `metric_threshold` (with `artifact_metric` accepted as an alias per the
//! gate-record contract enum), `receipt_flag`, `schema_check`,
//! `quinte_result` (Result 2.1 shape), and `artifact_flag`.
//!
//! Fail-closed contract: unknown gate kind, missing metric, unparsable
//! artifact, or a non-numeric value under a numeric comparison are
//! evaluation *errors* — the run halts regardless of the gate's configured
//! `on_fail`.

use std::path::Path;

use serde_json::Value;

use crate::error::AppError;

#[derive(Debug, Clone, PartialEq)]
pub enum GateKind {
    MetricThreshold,
    ReceiptFlag,
    SchemaCheck,
    QuinteResult,
    ArtifactFlag,
}

#[derive(Debug, Clone)]
pub struct GateDef {
    pub id: String,
    pub kind: GateKind,
    /// Kind tag as written in the record ("metric_threshold" |
    /// "artifact_metric" | "receipt_flag" | "schema_check" |
    /// "quinte_result" | "artifact_flag").
    pub kind_tag: String,
    pub artifact: Option<String>,
    pub metric: Option<String>,
    pub flag: Option<String>,
    pub schema: Option<String>,
    pub op: Option<String>,
    pub value: Option<Value>,
    /// "blocked" | "halted"
    pub on_fail: String,
}

pub fn parse_def(raw: &Value) -> Result<GateDef, AppError> {
    let id = raw
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::usage("gate_def_invalid", "gate has no id"))?
        .to_string();
    let tag = raw
        .get("kind")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::usage("gate_def_invalid", format!("gate '{id}' has no kind")))?;
    let kind = match tag {
        "metric_threshold" | "artifact_metric" => GateKind::MetricThreshold,
        "receipt_flag" => GateKind::ReceiptFlag,
        "schema_check" => GateKind::SchemaCheck,
        "quinte_result" => GateKind::QuinteResult,
        "artifact_flag" => GateKind::ArtifactFlag,
        other => {
            return Err(AppError::integrity(
                "gate_kind_unknown",
                format!("gate '{id}' has unknown kind '{other}'"),
            ))
        }
    };
    let on_fail = raw
        .get("on_fail")
        .and_then(Value::as_str)
        .unwrap_or("halted")
        .to_string();
    if on_fail != "blocked" && on_fail != "halted" {
        return Err(AppError::usage(
            "gate_def_invalid",
            format!("gate '{id}' has invalid on_fail '{on_fail}'"),
        ));
    }
    Ok(GateDef {
        id,
        kind,
        kind_tag: tag.to_string(),
        artifact: raw
            .get("artifact")
            .and_then(Value::as_str)
            .map(str::to_string),
        metric: raw
            .get("metric")
            .and_then(Value::as_str)
            .map(str::to_string),
        flag: raw.get("flag").and_then(Value::as_str).map(str::to_string),
        schema: raw
            .get("schema")
            .and_then(Value::as_str)
            .map(str::to_string),
        op: raw.get("op").and_then(Value::as_str).map(str::to_string),
        value: raw.get("value").cloned(),
        on_fail,
    })
}

/// What the gate read, decided, and why — everything needed for the record.
#[derive(Debug, Clone)]
pub struct GateOutcome {
    pub decision: &'static str, // "pass" | "fail"
    pub observed: Option<Value>,
    pub detail: String,
    /// Evaluation errors force "halted" (fail-closed); otherwise def.on_fail.
    pub effective_on_fail: &'static str,
    /// Digest of the evidence object the gate read (artifact or receipt).
    pub evidence_sha256: String,
}

/// Resolves an artifact name to (digest, bytes).
pub type ArtifactResolver<'a> = dyn Fn(&str) -> Result<(String, Vec<u8>), AppError> + 'a;

pub fn evaluate(
    def: &GateDef,
    resolve_artifact: &ArtifactResolver,
    receipts: &[(String, Vec<u8>)],
    doctrine_dir: &Path,
) -> Result<GateOutcome, AppError> {
    match def.kind {
        GateKind::MetricThreshold => eval_metric_threshold(def, resolve_artifact),
        GateKind::ReceiptFlag => eval_receipt_flag(def, receipts),
        GateKind::SchemaCheck => eval_schema_check(def, resolve_artifact, doctrine_dir),
        GateKind::QuinteResult => eval_quinte_result(def, resolve_artifact),
        GateKind::ArtifactFlag => eval_artifact_flag(def, resolve_artifact),
    }
}

fn eval_error(detail: impl Into<String>, evidence_sha256: String) -> GateOutcome {
    // Fail-closed: evaluation ambiguity halts, whatever on_fail says.
    GateOutcome {
        decision: "fail",
        observed: None,
        detail: detail.into(),
        effective_on_fail: "halted",
        evidence_sha256,
    }
}

fn eval_metric_threshold(
    def: &GateDef,
    resolve_artifact: &ArtifactResolver,
) -> Result<GateOutcome, AppError> {
    let artifact_name = match (&def.artifact, &def.metric, &def.op, &def.value) {
        (Some(a), Some(_), Some(_), Some(_)) => a.clone(),
        _ => {
            return Ok(eval_error(
                format!(
                "gate '{}' is malformed for metric_threshold (needs artifact, metric, op, value)",
                def.id
            ),
                empty_digest(),
            ))
        }
    };
    let (digest, bytes) = resolve_artifact(&artifact_name)?;
    let doc: Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(e) => {
            return Ok(eval_error(
                format!("artifact '{artifact_name}' unparsable: {e}"),
                digest,
            ))
        }
    };
    let metric = def.metric.as_deref().expect("checked above");
    let observed = match lookup_path(&doc, metric) {
        Some(v) => v.clone(),
        None => {
            return Ok(eval_error(
                format!("metric '{metric}' missing in artifact '{artifact_name}'"),
                digest,
            ))
        }
    };
    let (x, t) = match (
        observed.as_f64(),
        def.value.as_ref().and_then(Value::as_f64),
    ) {
        (Some(x), Some(t)) => (x, t),
        _ => {
            // Covers the NaN family: JSON cannot carry NaN, so any
            // non-numeric observed/threshold value under a numeric gate is
            // an evaluation error, never an ordering guess.
            return Ok(eval_error(
                format!(
                    "metric '{metric}' or threshold is non-numeric (observed={observed}, threshold={})",
                    def.value.clone().unwrap_or(Value::Null)
                ),
                digest,
            ));
        }
    };
    let op = def.op.as_deref().expect("checked above");
    let pass = compare_f64(op, x, t).ok_or_else(|| {
        AppError::integrity(
            "gate_op_unknown",
            format!("gate '{}' has unknown op '{op}'", def.id),
        )
    })?;
    Ok(GateOutcome {
        decision: if pass { "pass" } else { "fail" },
        observed: Some(observed),
        detail: format!("metric '{metric}' = {x}, threshold {op} {t}"),
        effective_on_fail: if def.on_fail == "blocked" {
            "blocked"
        } else {
            "halted"
        },
        evidence_sha256: digest,
    })
}

fn eval_receipt_flag(
    def: &GateDef,
    receipts: &[(String, Vec<u8>)],
) -> Result<GateOutcome, AppError> {
    let flag = match (&def.flag, &def.op, &def.value) {
        (Some(f), Some(_), Some(_)) => f.clone(),
        _ => {
            return Ok(eval_error(
                format!(
                    "gate '{}' is malformed for receipt_flag (needs flag, op, value)",
                    def.id
                ),
                empty_digest(),
            ))
        }
    };
    let (digest, bytes) = match receipts.first() {
        Some(r) => r.clone(),
        None => {
            return Ok(eval_error(
                format!("gate '{}' has no receipt to read", def.id),
                empty_digest(),
            ))
        }
    };
    let doc: Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(e) => return Ok(eval_error(format!("receipt unparsable: {e}"), digest)),
    };
    let observed = match lookup_path(&doc, &flag) {
        Some(v) => v.clone(),
        None => {
            return Ok(eval_error(
                format!("flag '{flag}' missing in receipt"),
                digest,
            ))
        }
    };
    let want = def.value.clone().expect("checked above");
    let op = def.op.as_deref().expect("checked above");
    let pass = match compare_values(op, &observed, &want) {
        Some(p) => p,
        None => {
            return Ok(eval_error(
                format!("flag '{flag}' comparison {op} not meaningful for {observed} vs {want}"),
                digest,
            ))
        }
    };
    let detail = format!("receipt flag '{flag}' = {observed}, expected {op} {want}");
    Ok(GateOutcome {
        decision: if pass { "pass" } else { "fail" },
        observed: Some(observed),
        detail,
        effective_on_fail: if def.on_fail == "blocked" {
            "blocked"
        } else {
            "halted"
        },
        evidence_sha256: digest,
    })
}

fn eval_schema_check(
    def: &GateDef,
    resolve_artifact: &ArtifactResolver,
    doctrine_dir: &Path,
) -> Result<GateOutcome, AppError> {
    let (artifact_name, schema_rel) = match (&def.artifact, &def.schema) {
        (Some(a), Some(s)) => (a.clone(), s.clone()),
        _ => {
            return Ok(eval_error(
                format!(
                    "gate '{}' is malformed for schema_check (needs artifact, schema)",
                    def.id
                ),
                empty_digest(),
            ))
        }
    };
    let (digest, bytes) = resolve_artifact(&artifact_name)?;
    let doc: Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(e) => {
            return Ok(eval_error(
                format!("artifact '{artifact_name}' unparsable: {e}"),
                digest,
            ))
        }
    };
    let schema_path = doctrine_dir.join(&schema_rel);
    let schema_text = match std::fs::read_to_string(&schema_path) {
        Ok(t) => t,
        Err(e) => {
            return Ok(eval_error(
                format!("schema file '{}' unreadable: {e}", schema_path.display()),
                digest,
            ))
        }
    };
    let schema: Value = match serde_json::from_str(&schema_text) {
        Ok(v) => v,
        Err(e) => {
            return Ok(eval_error(
                format!("schema file '{}' unparsable: {e}", schema_path.display()),
                digest,
            ))
        }
    };
    let errs = crate::jsonval::violations(&schema, &doc);
    let pass = errs.is_empty();
    Ok(GateOutcome {
        decision: if pass { "pass" } else { "fail" },
        observed: Some(Value::String(if pass {
            "valid".into()
        } else {
            "invalid".into()
        })),
        detail: if pass {
            format!("artifact '{artifact_name}' valid against {schema_rel}")
        } else {
            format!(
                "artifact '{artifact_name}' invalid against {schema_rel}: {}",
                errs.join("; ")
            )
        },
        effective_on_fail: if def.on_fail == "blocked" {
            "blocked"
        } else {
            "halted"
        },
        evidence_sha256: digest,
    })
}

/// Result 2.1 shape: `result_version`, `status`, `run_id`, `residuals`,
/// `recommendation`. Does not read `validation.walkforward.sharpe`.
pub fn result_21_shape(doc: &Value) -> Result<(), String> {
    match doc.get("result_version").and_then(Value::as_str) {
        Some("2.1") => {}
        Some(other) => return Err(format!("result_version is '{other}', expected '2.1'")),
        None => return Err("result_version missing".into()),
    }
    match doc.get("status").and_then(Value::as_str) {
        Some("completed") | Some("degraded") => {}
        Some(other) => {
            return Err(format!(
                "status is '{other}', expected 'completed' or 'degraded'"
            ))
        }
        None => return Err("status missing".into()),
    }
    match doc.get("run_id").and_then(Value::as_str) {
        Some(id) if !id.is_empty() => {}
        Some(_) => return Err("run_id is empty".into()),
        None => return Err("run_id missing".into()),
    }
    match doc.get("recommendation").and_then(Value::as_str) {
        Some(text) if !text.is_empty() => {}
        Some(_) => return Err("recommendation is empty".into()),
        None => return Err("recommendation missing".into()),
    }
    match doc.get("residuals") {
        Some(Value::Array(_)) => {}
        Some(_) => return Err("residuals is not an array".into()),
        None => return Err("residuals missing".into()),
    }
    // Honest labeling (RASHOMON response): the trial_manifest carries the
    // model-relation truth — a Result 2.1 without it cannot attest how its
    // perspectives relate, so the host fails closed rather than record an
    // unattested review. Mirrors QUINTE's own result schema requirement.
    match doc.get("trial_manifest").and_then(|t| t.get("base_model_relation")) {
        Some(Value::String(relation)) if !relation.is_empty() => {}
        Some(_) => return Err("trial_manifest.base_model_relation is not a non-empty string".into()),
        None => return Err("trial_manifest with base_model_relation missing".into()),
    }
    Ok(())
}

fn on_fail_of(def: &GateDef) -> &'static str {
    if def.on_fail == "blocked" {
        "blocked"
    } else {
        "halted"
    }
}

fn eval_quinte_result(
    def: &GateDef,
    resolve_artifact: &ArtifactResolver,
) -> Result<GateOutcome, AppError> {
    let artifact_name = match &def.artifact {
        Some(a) => a.clone(),
        None => {
            return Ok(eval_error(
                format!(
                    "gate '{}' is malformed for quinte_result (needs artifact)",
                    def.id
                ),
                empty_digest(),
            ))
        }
    };
    let (digest, bytes) = resolve_artifact(&artifact_name)?;
    let doc: Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(e) => {
            return Ok(eval_error(
                format!("artifact '{artifact_name}' unparsable: {e}"),
                digest,
            ))
        }
    };
    match result_21_shape(&doc) {
        Ok(()) => Ok(GateOutcome {
            decision: "pass",
            observed: Some(json_result21_observed(&doc)),
            detail: format!(
                "artifact '{artifact_name}' is a Result 2.1 (status={}, run_id present, residuals array, recommendation present)",
                doc.get("status").and_then(Value::as_str).unwrap_or("?")
            ),
            effective_on_fail: on_fail_of(def),
            evidence_sha256: digest,
        }),
        Err(detail) => Ok(GateOutcome {
            decision: "fail",
            observed: doc.get("result_version").cloned(),
            detail: format!("artifact '{artifact_name}' is not Result 2.1 shaped: {detail}"),
            effective_on_fail: on_fail_of(def),
            evidence_sha256: digest,
        }),
    }
}

fn json_result21_observed(doc: &Value) -> Value {
    // The trial_manifest fields surface the review's honesty data
    // (same-model caveat) into the gate record — hosts and downstream
    // reviewers must see them as evidence, never silently drop them.
    let tm = doc.get("trial_manifest");
    serde_json::json!({
        "result_version": doc.get("result_version").cloned().unwrap_or(Value::Null),
        "status": doc.get("status").cloned().unwrap_or(Value::Null),
        "run_id": doc.get("run_id").cloned().unwrap_or(Value::Null),
        "residual_count": doc.get("residuals").and_then(Value::as_array).map(|a| a.len()).unwrap_or(0),
        "base_model_relation": tm.and_then(|t| t.get("base_model_relation")).cloned().unwrap_or(Value::Null),
        "perspective_count": tm.and_then(|t| t.get("perspective_count")).cloned().unwrap_or(Value::Null),
        "contamination_risks": tm.and_then(|t| t.get("contamination_risks")).cloned().unwrap_or(Value::Null),
    })
}

fn eval_artifact_flag(
    def: &GateDef,
    resolve_artifact: &ArtifactResolver,
) -> Result<GateOutcome, AppError> {
    let (artifact_name, flag) = match (&def.artifact, &def.flag, &def.op, &def.value) {
        (Some(a), Some(f), Some(_), Some(_)) => (a.clone(), f.clone()),
        _ => {
            return Ok(eval_error(
                format!(
                    "gate '{}' is malformed for artifact_flag (needs artifact, flag, op, value)",
                    def.id
                ),
                empty_digest(),
            ))
        }
    };
    let (digest, bytes) = resolve_artifact(&artifact_name)?;
    let doc: Value = match serde_json::from_slice(&bytes) {
        Ok(v) => v,
        Err(e) => {
            return Ok(eval_error(
                format!("artifact '{artifact_name}' unparsable: {e}"),
                digest,
            ))
        }
    };
    let observed = match lookup_path(&doc, &flag) {
        Some(v) => v.clone(),
        None => {
            return Ok(eval_error(
                format!("flag '{flag}' missing in artifact '{artifact_name}'"),
                digest,
            ))
        }
    };
    let want = def.value.clone().expect("checked above");
    let op = def.op.as_deref().expect("checked above");
    let pass = match compare_values(op, &observed, &want) {
        Some(p) => p,
        None => {
            return Ok(eval_error(
                format!("flag '{flag}' comparison {op} not meaningful for {observed} vs {want}"),
                digest,
            ))
        }
    };
    Ok(GateOutcome {
        decision: if pass { "pass" } else { "fail" },
        observed: Some(observed.clone()),
        detail: format!("artifact flag '{flag}' = {observed}, expected {op} {want}"),
        effective_on_fail: on_fail_of(def),
        evidence_sha256: digest,
    })
}

/// The gate record, conforming to `stammtisch.gate-record.v0`.
pub fn build_record(def: &GateDef, run_id: &str, stage: &str, outcome: &GateOutcome) -> Value {
    let mut record = serde_json::json!({
        "schema": "stammtisch.gate-record.v0",
        "gate_id": def.id,
        "run_id": run_id,
        "stage": stage,
        "kind": def.kind_tag,
        "decision": outcome.decision,
        "on_fail": outcome.effective_on_fail,
        "artifact_sha256": outcome.evidence_sha256,
        "detail": outcome.detail,
        "evaluated_at": crate::time::now_rfc3339(),
    });
    if let Some(obs) = &outcome.observed {
        record["observed"] = obs.clone();
    }
    if let (Some(op), Some(value)) = (&def.op, &def.value) {
        record["threshold"] = serde_json::json!({"op": op, "value": value});
    }
    record
}

/// Dot-separated path lookup: "validation.walkforward.sharpe".
pub fn lookup_path<'a>(doc: &'a Value, path: &str) -> Option<&'a Value> {
    let mut cur = doc;
    for part in path.split('.') {
        cur = cur.get(part)?;
    }
    Some(cur)
}

fn compare_f64(op: &str, x: f64, t: f64) -> Option<bool> {
    match op {
        "==" => Some(x == t),
        "!=" => Some(x != t),
        ">" => Some(x > t),
        ">=" => Some(x >= t),
        "<" => Some(x < t),
        "<=" => Some(x <= t),
        _ => None,
    }
}

fn compare_values(op: &str, observed: &Value, want: &Value) -> Option<bool> {
    match (observed.as_f64(), want.as_f64()) {
        (Some(x), Some(t)) if matches!(op, ">" | ">=" | "<" | "<=") => compare_f64(op, x, t),
        _ => match op {
            "==" => Some(observed == want),
            "!=" => Some(observed != want),
            ">" | ">=" | "<" | "<=" => None, // ordering on non-numbers: fail-closed
            _ => None,
        },
    }
}

fn empty_digest() -> String {
    format!("sha256:{}", "0".repeat(64))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    type Resolver = Box<dyn Fn(&str) -> Result<(String, Vec<u8>), AppError>>;

    fn resolver_with(bytes: Vec<u8>) -> (String, Resolver) {
        let digest = crate::canon::sha256_prefixed(&bytes);
        let d = digest.clone();
        (
            digest,
            Box::new(move |_name: &str| Ok((d.clone(), bytes.clone()))),
        )
    }

    fn def(op: &str, value: Value) -> GateDef {
        parse_def(&json!({
            "id": "g", "kind": "metric_threshold", "artifact": "a.json",
            "metric": "validation.walkforward.sharpe", "op": op, "value": value,
            "on_fail": "blocked"
        }))
        .unwrap()
    }

    fn artifact(sharpe: Value) -> Vec<u8> {
        serde_json::to_vec(&json!({"validation": {"walkforward": {"sharpe": sharpe}}})).unwrap()
    }

    #[test]
    fn boundary_gte_equal_passes_gt_equal_fails() {
        let (d1, r1) = resolver_with(artifact(json!(0.0)));
        let gte = def(">=", json!(0.0));
        let out = evaluate(&gte, &r1, &[], Path::new(".")).unwrap();
        assert_eq!(out.decision, "pass");
        assert_eq!(out.evidence_sha256, d1);

        let (_d2, r2) = resolver_with(artifact(json!(0.0)));
        let gt = def(">", json!(0.0));
        let out = evaluate(&gt, &r2, &[], Path::new(".")).unwrap();
        assert_eq!(out.decision, "fail");
        assert_eq!(out.effective_on_fail, "blocked"); // ordinary fail honors on_fail
    }

    #[test]
    fn boundary_below_threshold() {
        let (_d, r) = resolver_with(artifact(json!(-0.0001)));
        let out = evaluate(&def(">=", json!(0.0)), &r, &[], Path::new(".")).unwrap();
        assert_eq!(out.decision, "fail");
    }

    #[test]
    fn missing_metric_is_fail_closed_halt() {
        let bytes = serde_json::to_vec(&json!({"validation": {}})).unwrap();
        let (_d, r) = resolver_with(bytes);
        let out = evaluate(&def(">=", json!(0.0)), &r, &[], Path::new(".")).unwrap();
        assert_eq!(out.decision, "fail");
        assert_eq!(out.effective_on_fail, "halted"); // eval error overrides on_fail=blocked
        assert!(out.detail.contains("missing"));
    }

    #[test]
    fn non_numeric_metric_is_fail_closed_halt() {
        // JSON has no NaN literal; a string "NaN" (or any non-number) under a
        // numeric gate must never be ordered — fail closed.
        let (_d, r) = resolver_with(artifact(json!("NaN")));
        let out = evaluate(&def(">=", json!(0.0)), &r, &[], Path::new(".")).unwrap();
        assert_eq!(out.decision, "fail");
        assert_eq!(out.effective_on_fail, "halted");

        let (_d2, r2) = resolver_with(artifact(json!(null)));
        let out2 = evaluate(&def(">=", json!(0.0)), &r2, &[], Path::new(".")).unwrap();
        assert_eq!(out2.decision, "fail");
        assert_eq!(out2.effective_on_fail, "halted");
    }

    #[test]
    fn unparsable_artifact_halts() {
        let (_d, r) = resolver_with(b"{not json".to_vec());
        let out = evaluate(&def(">=", json!(0.0)), &r, &[], Path::new(".")).unwrap();
        assert_eq!(out.decision, "fail");
        assert_eq!(out.effective_on_fail, "halted");
    }

    #[test]
    fn receipt_flag_eval() {
        let receipt = serde_json::to_vec(&json!({
            "schema": "highball.action-packet.v1", "decision": "AUTHORIZED",
            "packet_id": "pkt-1", "route": "direct-evidence"
        }))
        .unwrap();
        let digest = crate::canon::sha256_prefixed(&receipt);
        let pass_def = parse_def(&json!({
            "id": "packet_authorized", "kind": "receipt_flag", "flag": "decision",
            "op": "==", "value": "AUTHORIZED", "on_fail": "blocked"
        }))
        .unwrap();
        let out = evaluate(
            &pass_def,
            &|_| unreachable!(),
            &[(digest, receipt.clone())],
            Path::new("."),
        )
        .unwrap();
        assert_eq!(out.decision, "pass");

        let block_def = parse_def(&json!({
            "id": "packet_authorized", "kind": "receipt_flag", "flag": "decision",
            "op": "==", "value": "DENIED", "on_fail": "blocked"
        }))
        .unwrap();
        let d2 = crate::canon::sha256_prefixed(&receipt);
        let out = evaluate(
            &block_def,
            &|_| unreachable!(),
            &[(d2, receipt)],
            Path::new("."),
        )
        .unwrap();
        assert_eq!(out.decision, "fail");
        assert_eq!(out.effective_on_fail, "blocked");
    }

    #[test]
    fn schema_check_eval() {
        let dir = std::env::temp_dir().join(format!(
            "stammtisch-gates-{}",
            crate::ids::uuid_v7().unwrap()
        ));
        std::fs::create_dir_all(dir.join("schemas")).unwrap();
        std::fs::write(
            dir.join("schemas").join("r.schema.json"),
            r#"{"type":"object","required":["sharpe"],"properties":{"sharpe":{"type":"number","minimum":0}}}"#,
        )
        .unwrap();
        let check = parse_def(&json!({
            "id": "sc", "kind": "schema_check", "artifact": "a.json",
            "schema": "schemas/r.schema.json", "on_fail": "halted"
        }))
        .unwrap();

        let (_d, r) = resolver_with(serde_json::to_vec(&json!({"sharpe": 0.5})).unwrap());
        let out = evaluate(&check, &r, &[], &dir).unwrap();
        assert_eq!(out.decision, "pass");
        assert_eq!(out.observed, Some(json!("valid")));

        let (_d2, r2) = resolver_with(serde_json::to_vec(&json!({"sharpe": -1})).unwrap());
        let out = evaluate(&check, &r2, &[], &dir).unwrap();
        assert_eq!(out.decision, "fail");
        assert_eq!(out.observed, Some(json!("invalid")));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn unknown_kind_and_op_fail_closed() {
        let e = parse_def(&json!({"id": "x", "kind": "llm_vibes"})).unwrap_err();
        assert_eq!(e.code, "gate_kind_unknown");
        let bad_op = parse_def(&json!({
            "id": "g", "kind": "metric_threshold", "artifact": "a",
            "metric": "m", "op": "~=", "value": 1
        }))
        .unwrap();
        let (_d, r) = resolver_with(serde_json::to_vec(&json!({"m": 1})).unwrap());
        let e = evaluate(&bad_op, &r, &[], Path::new(".")).unwrap_err();
        assert_eq!(e.code, "gate_op_unknown");
    }

    fn result21(status: &str) -> Value {
        json!({
            "result_version": "2.1",
            "run_id": "run-result-21",
            "status": status,
            "brief_sha256": format!("sha256:{}", "a".repeat(64)),
            "question": "Should this design ship?",
            "action_scope": "service release",
            "affected_paths": ["service/config.json"],
            "action_binding_sha256": format!("sha256:{}", "b".repeat(64)),
            "seat_binding": {
                "seat_id": "seat-g",
                "family": "deepseek",
                "provider": "deepseek",
                "text_model": "deepseek",
                "multimodal_model": "deepseek"
            },
            "route_bindings": [
                {"party_id": "Party A", "route_id": "r-a", "adapter": "pi", "executable": "pi", "family": "deepseek", "provider": "deepseek", "text_model": "deepseek", "multimodal_model": "deepseek", "perspective": "A"},
                {"party_id": "Party B", "route_id": "r-b", "adapter": "pi", "executable": "pi", "family": "deepseek", "provider": "deepseek", "text_model": "deepseek", "multimodal_model": "deepseek", "perspective": "B"},
                {"party_id": "Party C", "route_id": "r-c", "adapter": "pi", "executable": "pi", "family": "deepseek", "provider": "deepseek", "text_model": "deepseek", "multimodal_model": "deepseek", "perspective": "C"},
                {"party_id": "Party D", "route_id": "r-d", "adapter": "pi", "executable": "pi", "family": "deepseek", "provider": "deepseek", "text_model": "deepseek", "multimodal_model": "deepseek", "perspective": "D"},
                {"party_id": "Party E", "route_id": "r-e", "adapter": "pi", "executable": "pi", "family": "deepseek", "provider": "deepseek", "text_model": "deepseek", "multimodal_model": "deepseek", "perspective": "E"},
                {"party_id": "Counterpart Arbiter", "route_id": "r-ca", "adapter": "pi", "executable": "pi", "family": "deepseek", "provider": "deepseek", "text_model": "deepseek", "multimodal_model": "deepseek", "perspective": "CA"},
                {"party_id": "Primary Arbiter", "route_id": "r-pa", "adapter": "pi", "executable": "pi", "family": "deepseek", "provider": "deepseek", "text_model": "deepseek", "multimodal_model": "deepseek", "perspective": "PA"}
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

    fn quinte_result_def() -> GateDef {
        parse_def(&json!({
            "id": "quinte_result_21",
            "kind": "quinte_result",
            "artifact": "review.result",
            "on_fail": "blocked"
        }))
        .unwrap()
    }

    #[test]
    fn result_21_without_walkforward_sharpe_passes_shaped_gate() {
        let doc = result21("completed");
        assert!(doc.pointer("/validation/walkforward/sharpe").is_none());
        result_21_shape(&doc).expect("representative Result 2.1 must satisfy the shape gate");
        let (_d, r) = resolver_with(serde_json::to_vec(&doc).unwrap());
        let out = evaluate(&quinte_result_def(), &r, &[], Path::new(".")).unwrap();
        assert_eq!(out.decision, "pass");
        assert_eq!(out.effective_on_fail, "blocked");
    }

    #[test]
    fn quinte_gate_surfaces_trial_manifest_honesty_fields() {
        let mut doc = result21("completed");
        doc["trial_manifest"] = json!({
            "base_model_relation": "same_model",
            "perspective_count": 5,
            "contamination_risks": ["same-family: not independent confirmation"]
        });
        let (_d, r) = resolver_with(serde_json::to_vec(&doc).unwrap());
        let out = evaluate(&quinte_result_def(), &r, &[], Path::new(".")).unwrap();
        assert_eq!(out.decision, "pass");
        let observed = out.observed.expect("observed evidence must carry the caveat");
        assert_eq!(observed["base_model_relation"], "same_model");
        assert_eq!(observed["perspective_count"], 5);
        assert_eq!(observed["contamination_risks"][0], "same-family: not independent confirmation");
    }

    #[test]
    fn result_21_still_fail_closes_walkforward_min_sharpe() {
        let doc = result21("completed");
        let (_d, r) = resolver_with(serde_json::to_vec(&doc).unwrap());
        let out = evaluate(&def(">=", json!(0.0)), &r, &[], Path::new(".")).unwrap();
        assert_eq!(out.decision, "fail");
        assert_eq!(out.effective_on_fail, "halted");
        assert!(out.detail.contains("missing"));
    }

    #[test]
    fn result_21_requires_honest_labeling() {
        let mut unlabeled = result21("completed");
        unlabeled.as_object_mut().unwrap().remove("trial_manifest");
        assert!(result_21_shape(&unlabeled)
            .unwrap_err()
            .contains("trial_manifest"));
        let (_d, r) = resolver_with(serde_json::to_vec(&unlabeled).unwrap());
        let out = evaluate(&quinte_result_def(), &r, &[], Path::new(".")).unwrap();
        assert_eq!(out.decision, "fail");
        assert_eq!(out.effective_on_fail, "blocked");
    }

    #[test]
    fn result_21_rejects_wrong_version_and_missing_fields() {
        let mut bad = result21("completed");
        bad["result_version"] = json!("2.0");
        assert!(result_21_shape(&bad).is_err());
        let (_d, r) = resolver_with(serde_json::to_vec(&bad).unwrap());
        let out = evaluate(&quinte_result_def(), &r, &[], Path::new(".")).unwrap();
        assert_eq!(out.decision, "fail");
        assert_eq!(out.effective_on_fail, "blocked");

        let mut missing = result21("degraded");
        missing.as_object_mut().unwrap().remove("residuals");
        assert!(result_21_shape(&missing).unwrap_err().contains("residuals"));
    }

    #[test]
    fn artifact_flag_reads_action_decision() {
        let bytes = serde_json::to_vec(&json!({
            "packet_version": "2.0",
            "action_decision": "pass"
        }))
        .unwrap();
        let (_d, r) = resolver_with(bytes);
        let pass_def = parse_def(&json!({
            "id": "packet_authorized", "kind": "artifact_flag",
            "artifact": "deliver.packet.json", "flag": "action_decision",
            "op": "==", "value": "pass", "on_fail": "blocked"
        }))
        .unwrap();
        let out = evaluate(&pass_def, &r, &[], Path::new(".")).unwrap();
        assert_eq!(out.decision, "pass");

        let block_bytes = serde_json::to_vec(&json!({
            "packet_version": "2.0",
            "action_decision": "block"
        }))
        .unwrap();
        let (_d2, r2) = resolver_with(block_bytes);
        let out = evaluate(&pass_def, &r2, &[], Path::new(".")).unwrap();
        assert_eq!(out.decision, "fail");
        assert_eq!(out.effective_on_fail, "blocked");
    }

    #[test]
    fn record_conforms_to_schema() {
        let (_d, r) = resolver_with(artifact(json!(0.42)));
        let g = def(">=", json!(0.0));
        let out = evaluate(&g, &r, &[], Path::new(".")).unwrap();
        let record = build_record(&g, "019b4e5a-2c3d-7e8f-9a0b-1c2d3e4f5a6b", "review", &out);
        let schema: Value = serde_json::from_str(crate::schemas::GATE_RECORD).unwrap();
        assert_eq!(
            crate::jsonval::violations(&schema, &record),
            Vec::<String>::new()
        );
    }
}
