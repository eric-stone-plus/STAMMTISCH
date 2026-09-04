//! Shipped GALAHAD adapter: `galahad-futures` paper session
//! (`scripts/run_paper.py --source fixture --json`) plus a controlled
//! Binance TESTNET pass-through (`--engine nautilus_live`, summary
//! `mode: "testnet"`, gated by `GALAHAD_ENABLE_TESTNET=1`). DoctrineFake
//! stays on `adapter: "fake"`. Live capital is refused everywhere.

use std::cell::RefCell;
use std::path::PathBuf;

use serde_json::{json, Value};

use super::{Adapter, Collected, InvocationHandle, PollState, StageContext, Verdict};
use crate::canon;
use crate::error::AppError;
use crate::pipeline::Stage;

pub struct GalahadAdapter {
    root: PathBuf,
    script: PathBuf,
    session: RefCell<Option<Value>>,
}

impl GalahadAdapter {
    pub fn new(stage: &Stage) -> Result<Self, AppError> {
        let root = resolve_root(stage)?;
        let script = root.join("scripts").join("run_paper.py");
        if !script.is_file() {
            return Err(AppError::usage(
                "galahad_workdir_required",
                format!(
                    "stage '{}' workdir {} is not a galahad-futures tree (missing scripts/run_paper.py)",
                    stage.id,
                    root.display()
                ),
            ));
        }
        Ok(Self {
            root,
            script,
            session: RefCell::new(None),
        })
    }
}

fn resolve_root(stage: &Stage) -> Result<PathBuf, AppError> {
    if let Some(dir) = &stage.workdir {
        return Ok(dir.clone());
    }
    if let Ok(home) = std::env::var("GALAHAD_HOME") {
        let futures = PathBuf::from(&home).join("galahad-futures");
        if futures.join("scripts").join("run_paper.py").is_file() {
            return Ok(futures);
        }
        let home_p = PathBuf::from(home);
        if home_p.join("scripts").join("run_paper.py").is_file() {
            return Ok(home_p);
        }
    }
    Err(AppError::usage(
        "galahad_workdir_required",
        format!(
            "stage '{}' product galahad requires workdir (galahad-futures root) or GALAHAD_HOME",
            stage.id
        ),
    ))
}

/// The session mode a GALAHAD summary claims, after the fail-closed
/// mode decision.
enum SessionMode {
    Paper,
    Testnet,
}

/// The testnet pass-through is env-gated at adapter run time. The helper
/// exists so the mode decision in `map_session` takes the value as a
/// parameter and tests stay deterministic.
fn testnet_enabled() -> bool {
    std::env::var("GALAHAD_ENABLE_TESTNET").ok().as_deref() == Some("1")
}

/// Fail-closed mode decision. `"paper"` passes as before. `"testnet"` is
/// a controlled Binance testnet pass-through and requires ALL THREE of:
/// the stage declaring `engine: "nautilus_live"`, the summary mode, and
/// `GALAHAD_ENABLE_TESTNET=1` at adapter run time (passed in as
/// `testnet_enabled`). Any other mode string — mainnet `"live"`
/// included — stays refused.
fn decide_mode(
    mode: &str,
    declared_engine: Option<&str>,
    testnet_enabled: bool,
) -> Result<SessionMode, AppError> {
    match mode {
        "paper" => Ok(SessionMode::Paper),
        "testnet" => {
            if declared_engine != Some("nautilus_live") {
                return Err(AppError::product(
                    "galahad_testnet_undeclared",
                    "GALAHAD reported a testnet session but the stage does not declare engine 'nautilus_live'",
                ));
            }
            if !testnet_enabled {
                return Err(AppError::usage(
                    "galahad_testnet_disabled",
                    "GALAHAD testnet pass-through requires GALAHAD_ENABLE_TESTNET=1 at adapter run time",
                ));
            }
            Ok(SessionMode::Testnet)
        }
        other => Err(AppError::product(
            "galahad_live_refused",
            format!("GALAHAD mode is '{other}'; only paper and gated testnet are accepted"),
        )),
    }
}

/// Testnet sessions must carry the venue and the reconciliation fields the
/// `galahad.testnet-session.v1` receipt pins; their absence is fail-closed.
fn check_testnet_fields(summary: &Value) -> Result<(), AppError> {
    let invalid = |detail: &str| {
        AppError::product(
            "galahad_session_invalid",
            format!("GALAHAD testnet summary {detail}"),
        )
    };
    summary
        .get("venue")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| invalid("has no venue"))?;
    let reconciliation = summary
        .get("reconciliation")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid("has no reconciliation object"))?;
    for key in ["orders_submitted", "orders_filled"] {
        if reconciliation.get(key).and_then(Value::as_i64).is_none() {
            return Err(invalid("reconciliation is missing an integer order count"));
        }
    }
    if reconciliation
        .get("position_mismatch")
        .and_then(Value::as_bool)
        .is_none()
    {
        return Err(invalid("reconciliation is missing position_mismatch"));
    }
    Ok(())
}

/// Map a GALAHAD session summary (and optional journal positions) into the
/// stage artifact + verdict. Missing or unparseable output fail-closes.
/// The mode decision gates the testnet pass-through; live mode is refused.
pub fn map_session(
    summary: Option<&Value>,
    positions: Option<&Value>,
    declared_engine: Option<&str>,
    testnet_enabled: bool,
) -> Result<(Value, Verdict), AppError> {
    let summary = summary.ok_or_else(missing_session)?;
    if !summary.is_object() {
        return Err(missing_session());
    }
    let run_id = summary
        .get("run_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            AppError::product(
                "galahad_session_invalid",
                "GALAHAD session summary has no run_id",
            )
        })?;
    let symbol = summary
        .get("symbol")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            AppError::product(
                "galahad_session_invalid",
                "GALAHAD session summary has no symbol",
            )
        })?;
    let status = summary
        .get("status")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            AppError::product(
                "galahad_session_invalid",
                "GALAHAD session summary has no status",
            )
        })?;
    let mode = summary
        .get("mode")
        .and_then(Value::as_str)
        .unwrap_or("paper");
    if matches!(
        decide_mode(mode, declared_engine, testnet_enabled)?,
        SessionMode::Testnet
    ) {
        check_testnet_fields(summary)?;
    }
    let liquidated = summary
        .get("liquidated")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let nogo = liquidated
        || status == "no-trade but risk-idle OK"
        || status == "ok_invalidated"
        || status.starts_with("no-trade");
    let verdict_tag = if nogo { "NO-GO" } else { "GO" };
    if !nogo && status != "ok" {
        return Err(AppError::product(
            "galahad_session_invalid",
            format!("GALAHAD session status '{status}' is not a known session outcome"),
        ));
    }
    let targets = positions.cloned().unwrap_or_else(|| json!({}));
    let mut artifact = summary.clone();
    artifact["as_of"] = json!(run_id);
    artifact["verdict"] = json!(verdict_tag);
    artifact["targets"] = targets;
    artifact["selection"] = json!({
        "run_id": run_id,
        "symbol": symbol,
        "strategy": summary.get("strategy").cloned().unwrap_or(Value::Null),
        "source_used": summary.get("source_used").cloned().unwrap_or(Value::Null),
        "as_of": run_id,
    });
    let out_verdict = if nogo {
        Verdict::Refused("NO-GO".into())
    } else {
        Verdict::Proceed
    };
    Ok((artifact, out_verdict))
}

pub fn missing_session() -> AppError {
    AppError::product(
        "galahad_session_missing",
        "GALAHAD produced no session JSON; absence of output is fail-closed",
    )
}

/// Append the `--engine` argument pair for a stage-declared backend.
///
/// The nautilus backend is an optional dependency in the galahad-futures
/// tree; its absence surfaces as a product error (fail closed), never a
/// silent fallback to the paper book. `nautilus_live` is the gated Binance
/// testnet pass-through (see `decide_mode`).
fn push_engine_arg<'a>(stage: &'a Stage, args: &mut Vec<&'a str>) -> Result<(), AppError> {
    let Some(engine) = &stage.engine else {
        return Ok(());
    };
    if engine != "paper" && engine != "nautilus" && engine != "nautilus_live" {
        return Err(AppError::usage(
            "galahad_engine_invalid",
            format!(
                "stage '{}' engine '{engine}' is not paper|nautilus|nautilus_live",
                stage.id
            ),
        ));
    }
    args.push("--engine");
    args.push(engine.as_str());
    Ok(())
}

/// A stage declaring the gated testnet backend without the env gate set
/// fails before any product contact.
fn ensure_testnet_gate(stage: &Stage) -> Result<(), AppError> {
    if stage.engine.as_deref() == Some("nautilus_live") && !testnet_enabled() {
        return Err(AppError::usage(
            "galahad_testnet_disabled",
            format!(
                "stage '{}' declares engine 'nautilus_live' but GALAHAD_ENABLE_TESTNET=1 is not set",
                stage.id
            ),
        ));
    }
    Ok(())
}

/// Engine identity is evidence: the backend that produced the summary
/// must match the declared stage parameter ("paper", "nautilus", or the
/// gated "nautilus_live"). Summaries from galahad-futures predating the
/// engine field default to "paper" (backward compatible).
fn check_engine_identity(stage: &Stage, summary: &Value) -> Result<(), AppError> {
    let Some(declared) = &stage.engine else {
        return Ok(());
    };
    let reported = summary.get("engine").and_then(Value::as_str).unwrap_or("paper");
    if reported != declared {
        return Err(AppError::product(
            "galahad_engine_mismatch",
            format!(
                "stage '{}' declared engine '{declared}' but the product reported '{reported}'",
                stage.id
            ),
        ));
    }
    Ok(())
}

fn receipt(artifact: &Value) -> Value {
    let base = json!({
        "run_id": artifact.get("run_id").and_then(Value::as_str).unwrap_or(""),
        "symbol": artifact.get("symbol").and_then(Value::as_str).unwrap_or(""),
        "verdict": artifact.get("verdict").and_then(Value::as_str).unwrap_or("NO-GO"),
        "summary_sha256": canon::sha256_value_prefixed(artifact),
    });
    if artifact.get("mode").and_then(Value::as_str) == Some("testnet") {
        let mut r = base;
        r["schema"] = json!("galahad.testnet-session.v1");
        r["mode"] = json!("testnet");
        r["venue"] = artifact.get("venue").cloned().unwrap_or(Value::Null);
        r["reconciliation"] = artifact
            .get("reconciliation")
            .cloned()
            .unwrap_or_else(|| json!({}));
        return r;
    }
    let mut r = base;
    r["schema"] = json!("galahad.paper-session.v1");
    r["mode"] = json!("paper");
    r
}

fn load_positions(summary: &Value) -> Result<Option<Value>, AppError> {
    let Some(path) = summary.get("journal_path").and_then(Value::as_str) else {
        return Ok(None);
    };
    let text = std::fs::read_to_string(path).map_err(|e| {
        AppError::product(
            "galahad_session_invalid",
            format!("GALAHAD journal {path} unreadable: {e}"),
        )
    })?;
    let journal: Value = serde_json::from_str(&text).map_err(|e| {
        AppError::product(
            "galahad_session_invalid",
            format!("GALAHAD journal {path} is not JSON: {e}"),
        )
    })?;
    Ok(journal.get("positions").cloned())
}

impl Adapter for GalahadAdapter {
    fn preflight(&self, _ctx: &StageContext) -> Result<(), AppError> {
        if !self.script.is_file() {
            return Err(AppError::product(
                "adapter_preflight_failed",
                format!("GALAHAD script {} is not a file", self.script.display()),
            ));
        }
        if !self.root.is_dir() {
            return Err(AppError::product(
                "adapter_preflight_failed",
                format!("GALAHAD root {} is not a directory", self.root.display()),
            ));
        }
        Ok(())
    }

    fn invoke(&self, ctx: &StageContext) -> Result<InvocationHandle, AppError> {
        let out_dir = ctx.run_dir.join("galahad-out");
        std::fs::create_dir_all(&out_dir)?;
        let out_s = out_dir.to_str().unwrap_or("");
        let python = super::cli::resolve_python("GALAHAD_PYTHON")?;
        let script_s = self.script.to_str().unwrap_or("");
        let mut args: Vec<&str> = vec![
            script_s,
            "--source",
            "fixture",
            "--json",
            "--output-dir",
            out_s,
            "--strategy",
            "dual_ma",
        ];
        push_engine_arg(ctx.stage, &mut args)?;
        ensure_testnet_gate(ctx.stage)?;
        let (_code, summary) = super::cli::run_json(&python, &args)?;
        check_engine_identity(ctx.stage, &summary)?;
        let positions = load_positions(&summary)?;
        let (artifact, _verdict) = map_session(
            Some(&summary),
            positions.as_ref(),
            ctx.stage.engine.as_deref(),
            testnet_enabled(),
        )?;
        *self.session.borrow_mut() = Some(artifact);
        Ok(InvocationHandle {
            id: format!("galahad-paper-{}", &ctx.run_id[..8.min(ctx.run_id.len())]),
            product: "galahad".into(),
        })
    }

    fn poll(&self, _h: &InvocationHandle) -> PollState {
        if self.session.borrow().is_some() {
            PollState::Completed
        } else {
            PollState::Failed("GALAHAD produced no paper session".into())
        }
    }

    fn collect(&self, _h: &InvocationHandle, ctx: &StageContext) -> Result<Collected, AppError> {
        let artifact = self.session.borrow().clone().ok_or_else(missing_session)?;
        let verdict = match artifact.get("verdict").and_then(Value::as_str) {
            Some("GO") => Verdict::Proceed,
            Some(other) => Verdict::Refused(other.to_string()),
            None => return Err(missing_session()),
        };
        let name = ctx
            .stage
            .outputs
            .first()
            .cloned()
            .unwrap_or_else(|| "galahad.summary.json".into());
        Ok(Collected {
            receipts: vec![receipt(&artifact)],
            artifacts: vec![(name, artifact)],
            verdict,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn summary(status: &str) -> Value {
        json!({
            "run_id": "20260814T000000Z",
            "mode": "paper",
            "strategy": "dual_ma",
            "symbol": "BTCUSDT",
            "source_used": "fixture",
            "status": status,
            "n_fills": if status == "ok" { 2 } else { 0 },
            "liquidated": false
        })
    }

    #[test]
    fn map_ok_is_go_with_identity_and_targets() {
        let pos = json!({"BTCUSDT": {"side": "short", "qty": -0.3}});
        let (art, v) = map_session(Some(&summary("ok")), Some(&pos), None, false).unwrap();
        assert!(matches!(v, Verdict::Proceed));
        assert_eq!(art["verdict"], "GO");
        assert_eq!(art["as_of"], "20260814T000000Z");
        assert_eq!(art["selection"]["symbol"], "BTCUSDT");
        assert_eq!(art["targets"]["BTCUSDT"]["side"], "short");
        assert_eq!(art["run_id"], "20260814T000000Z");
    }

    #[test]
    fn map_no_trade_is_nogo() {
        let (art, v) = map_session(
            Some(&summary("no-trade but risk-idle OK")),
            None,
            None,
            false,
        )
        .unwrap();
        assert!(matches!(v, Verdict::Refused(ref s) if s == "NO-GO"));
        assert_eq!(art["verdict"], "NO-GO");
    }

    #[test]
    fn missing_and_live_fail_close() {
        assert_eq!(
            map_session(None, None, None, false).unwrap_err().code,
            "galahad_session_missing"
        );
        let mut live = summary("ok");
        live["mode"] = json!("live");
        assert_eq!(
            map_session(Some(&live), None, None, false)
                .unwrap_err()
                .code,
            "galahad_live_refused"
        );
        let mut bare = summary("ok");
        bare.as_object_mut().unwrap().remove("run_id");
        assert_eq!(
            map_session(Some(&bare), None, None, false)
                .unwrap_err()
                .code,
            "galahad_session_invalid"
        );
    }
}

#[cfg(test)]
mod engine_tests {
    use super::*;
    use crate::pipeline;

    fn stage_with(engine: Option<&str>) -> Stage {
        let mut stage_json = json!({
            "id": "s1",
            "product": "galahad",
            "out": ["galahad.summary.json"]
        });
        if let Some(e) = engine {
            stage_json["engine"] = json!(e);
        }
        let v = json!({
            "schema": "stammtisch.pipeline.v0",
            "id": "t-engine",
            "doctrine": {"pack": "galahad"},
            "stages": [stage_json]
        });
        pipeline::validate(&v, std::path::Path::new("x.json"))
            .unwrap()
            .stages
            .into_iter()
            .next()
            .unwrap()
    }

    #[test]
    fn engine_arg_defaults_to_no_flag() {
        let s = stage_with(None);
        let mut args: Vec<&str> = vec![];
        push_engine_arg(&s, &mut args).unwrap();
        assert!(args.is_empty());
    }

    #[test]
    fn engine_arg_passes_nautilus() {
        let s = stage_with(Some("nautilus"));
        let mut args: Vec<&str> = vec!["script"];
        push_engine_arg(&s, &mut args).unwrap();
        assert_eq!(args, vec!["script", "--engine", "nautilus"]);
    }

    #[test]
    fn engine_arg_passes_nautilus_live() {
        let s = stage_with(Some("nautilus_live"));
        let mut args: Vec<&str> = vec!["script"];
        push_engine_arg(&s, &mut args).unwrap();
        assert_eq!(args, vec!["script", "--engine", "nautilus_live"]);
    }

    #[test]
    fn engine_identity_mismatch_fails_closed() {
        let s = stage_with(Some("nautilus"));
        let summary = json!({"engine": "paper", "run_id": "x"});
        let err = check_engine_identity(&s, &summary).unwrap_err();
        assert_eq!(err.code, "galahad_engine_mismatch");
        // The gated testnet backend must match exactly too.
        let s = stage_with(Some("nautilus_live"));
        let summary = json!({"engine": "nautilus", "run_id": "x"});
        let err = check_engine_identity(&s, &summary).unwrap_err();
        assert_eq!(err.code, "galahad_engine_mismatch");
    }

    #[test]
    fn engine_identity_accepts_legacy_summaries() {
        let s = stage_with(Some("paper"));
        check_engine_identity(&s, &json!({"run_id": "x"})).unwrap();
        let s2 = stage_with(Some("nautilus"));
        check_engine_identity(&s2, &json!({"engine": "nautilus"})).unwrap();
        let s3 = stage_with(Some("nautilus_live"));
        check_engine_identity(&s3, &json!({"engine": "nautilus_live"})).unwrap();
    }
}

#[cfg(test)]
mod testnet_tests {
    use super::*;
    use serde_json::json;

    /// A product summary for a gated Binance testnet session, per the
    /// cross-repo contract: `mode: "testnet"` (never "live"),
    /// `engine: "nautilus_live"`, pinned engine version, venue, and the
    /// reconciliation digest fields.
    fn testnet_summary(status: &str) -> Value {
        json!({
            "run_id": "20260904T000000Z",
            "mode": "testnet",
            "engine": "nautilus_live",
            "engine_version": "nautilus_trader-1.231.0",
            "venue": "BINANCE",
            "strategy": "dual_ma",
            "symbol": "BTCUSDT",
            "source_used": "fixture",
            "status": status,
            "n_fills": if status == "ok" { 2 } else { 0 },
            "liquidated": false,
            "reconciliation": {
                "orders_submitted": 3,
                "orders_filled": 2,
                "position_mismatch": false
            }
        })
    }

    #[test]
    fn testnet_accepted_when_all_three_conditions_hold() {
        let (art, v) = map_session(
            Some(&testnet_summary("ok")),
            None,
            Some("nautilus_live"),
            true,
        )
        .unwrap();
        assert!(matches!(v, Verdict::Proceed));
        assert_eq!(art["verdict"], "GO");
        assert_eq!(art["mode"], "testnet");
        assert_eq!(art["venue"], "BINANCE");
        assert_eq!(art["reconciliation"]["orders_filled"], 2);
    }

    #[test]
    fn testnet_no_trade_is_nogo() {
        let (art, v) = map_session(
            Some(&testnet_summary("no-trade but risk-idle OK")),
            None,
            Some("nautilus_live"),
            true,
        )
        .unwrap();
        assert!(matches!(v, Verdict::Refused(ref s) if s == "NO-GO"));
        assert_eq!(art["verdict"], "NO-GO");
    }

    #[test]
    fn testnet_refused_without_env_gate() {
        let err = map_session(
            Some(&testnet_summary("ok")),
            None,
            Some("nautilus_live"),
            false,
        )
        .unwrap_err();
        assert_eq!(err.code, "galahad_testnet_disabled");
    }

    #[test]
    fn testnet_refused_without_declared_engine() {
        for declared in [None, Some("paper"), Some("nautilus")] {
            let err = map_session(Some(&testnet_summary("ok")), None, declared, true).unwrap_err();
            assert_eq!(err.code, "galahad_testnet_undeclared", "declared={declared:?}");
        }
    }

    #[test]
    fn live_refused_even_with_engine_and_env() {
        let mut live = testnet_summary("ok");
        live["mode"] = json!("live");
        let err = map_session(Some(&live), None, Some("nautilus_live"), true).unwrap_err();
        assert_eq!(err.code, "galahad_live_refused");
        // Any other unknown mode string fails closed the same way.
        let mut demo = testnet_summary("ok");
        demo["mode"] = json!("demo");
        let err = map_session(Some(&demo), None, Some("nautilus_live"), true).unwrap_err();
        assert_eq!(err.code, "galahad_live_refused");
    }

    #[test]
    fn testnet_missing_reconciliation_fails_closed() {
        let mut bare = testnet_summary("ok");
        bare.as_object_mut().unwrap().remove("reconciliation");
        let err = map_session(Some(&bare), None, Some("nautilus_live"), true).unwrap_err();
        assert_eq!(err.code, "galahad_session_invalid");
        let mut no_venue = testnet_summary("ok");
        no_venue.as_object_mut().unwrap().remove("venue");
        let err = map_session(Some(&no_venue), None, Some("nautilus_live"), true).unwrap_err();
        assert_eq!(err.code, "galahad_session_invalid");
    }

    #[test]
    fn receipts_carry_mode_schema_ids() {
        let paper = json!({
            "run_id": "r1", "mode": "paper", "symbol": "BTCUSDT",
            "status": "ok", "liquidated": false
        });
        let (art, _) = map_session(Some(&paper), None, None, false).unwrap();
        let r = receipt(&art);
        assert_eq!(r["schema"], "galahad.paper-session.v1");
        crate::contracts::validate_receipt(crate::canon::canonical(&r).as_bytes()).unwrap();

        let (art, _) = map_session(
            Some(&testnet_summary("ok")),
            None,
            Some("nautilus_live"),
            true,
        )
        .unwrap();
        let r = receipt(&art);
        assert_eq!(r["schema"], "galahad.testnet-session.v1");
        assert_eq!(r["mode"], "testnet");
        assert_eq!(r["venue"], "BINANCE");
        assert_eq!(r["reconciliation"]["orders_submitted"], 3);
        crate::contracts::validate_receipt(crate::canon::canonical(&r).as_bytes()).unwrap();
    }

    // GALAHAD_ENABLE_TESTNET is process-global, so the env-helper test is
    // serialized by a mutex and restores on drop (same pattern as the
    // highball adapter tests).
    struct EnvGuard {
        _lock: std::sync::MutexGuard<'static, ()>,
        saved: Option<std::ffi::OsString>,
    }

    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    impl EnvGuard {
        fn set(value: Option<&str>) -> Self {
            let lock = ENV_LOCK.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            let saved = std::env::var_os("GALAHAD_ENABLE_TESTNET");
            match value {
                Some(v) => std::env::set_var("GALAHAD_ENABLE_TESTNET", v),
                None => std::env::remove_var("GALAHAD_ENABLE_TESTNET"),
            }
            EnvGuard { _lock: lock, saved }
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            match self.saved.take() {
                Some(v) => std::env::set_var("GALAHAD_ENABLE_TESTNET", v),
                None => std::env::remove_var("GALAHAD_ENABLE_TESTNET"),
            }
        }
    }

    #[test]
    fn env_helper_reads_gate_exactly() {
        {
            let _g = EnvGuard::set(Some("1"));
            assert!(testnet_enabled());
        }
        {
            let _g = EnvGuard::set(None);
            assert!(!testnet_enabled());
        }
        {
            let _g = EnvGuard::set(Some("0"));
            assert!(!testnet_enabled());
        }
        let _g = EnvGuard::set(Some("yes"));
        assert!(!testnet_enabled());
    }
}
