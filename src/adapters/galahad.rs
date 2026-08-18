//! Shipped GALAHAD adapter: `galahad-futures` paper session
//! (`scripts/run_paper.py --source fixture --json`). DoctrineFake stays
//! on `adapter: "fake"`. Live capital is refused.

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

/// Map a GALAHAD paper-session summary (and optional journal positions)
/// into the stage artifact + verdict. Missing or unparseable output
/// fail-closes. Live mode is refused.
pub fn map_paper_session(
    summary: Option<&Value>,
    positions: Option<&Value>,
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
                "GALAHAD paper summary has no run_id",
            )
        })?;
    let symbol = summary
        .get("symbol")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            AppError::product(
                "galahad_session_invalid",
                "GALAHAD paper summary has no symbol",
            )
        })?;
    let status = summary
        .get("status")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            AppError::product(
                "galahad_session_invalid",
                "GALAHAD paper summary has no status",
            )
        })?;
    let mode = summary
        .get("mode")
        .and_then(Value::as_str)
        .unwrap_or("paper");
    if mode != "paper" {
        return Err(AppError::product(
            "galahad_live_refused",
            format!("GALAHAD mode is '{mode}'; only paper is accepted"),
        ));
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
            format!("GALAHAD paper status '{status}' is not a known paper outcome"),
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
        "GALAHAD produced no paper-session JSON; absence of output is fail-closed",
    )
}

/// Append the `--engine` argument pair for a stage-declared backend.
///
/// The nautilus backend is an optional dependency in the galahad-futures
/// tree; its absence surfaces as a product error (fail closed), never a
/// silent fallback to the paper book.
fn push_engine_arg<'a>(stage: &'a Stage, args: &mut Vec<&'a str>) -> Result<(), AppError> {
    let Some(engine) = &stage.engine else {
        return Ok(());
    };
    if engine != "paper" && engine != "nautilus" {
        return Err(AppError::usage(
            "galahad_engine_invalid",
            format!("stage '{}' engine '{engine}' is not paper|nautilus", stage.id),
        ));
    }
    args.push("--engine");
    args.push(engine.as_str());
    Ok(())
}

/// Engine identity is evidence: the backend that produced the summary
/// must match the declared stage parameter. Summaries from galahad-futures
/// predating the engine field default to "paper" (backward compatible).
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
    json!({
        "schema": "galahad.paper-session.v1",
        "run_id": artifact.get("run_id").and_then(Value::as_str).unwrap_or(""),
        "symbol": artifact.get("symbol").and_then(Value::as_str).unwrap_or(""),
        "verdict": artifact.get("verdict").and_then(Value::as_str).unwrap_or("NO-GO"),
        "mode": "paper",
        "summary_sha256": canon::sha256_value_prefixed(artifact),
    })
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
        let (_code, summary) = super::cli::run_json(&python, &args)?;
        check_engine_identity(ctx.stage, &summary)?;
        let positions = load_positions(&summary)?;
        let (artifact, _verdict) = map_paper_session(Some(&summary), positions.as_ref())?;
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
        let (art, v) = map_paper_session(Some(&summary("ok")), Some(&pos)).unwrap();
        assert!(matches!(v, Verdict::Proceed));
        assert_eq!(art["verdict"], "GO");
        assert_eq!(art["as_of"], "20260814T000000Z");
        assert_eq!(art["selection"]["symbol"], "BTCUSDT");
        assert_eq!(art["targets"]["BTCUSDT"]["side"], "short");
        assert_eq!(art["run_id"], "20260814T000000Z");
    }

    #[test]
    fn map_no_trade_is_nogo() {
        let (art, v) =
            map_paper_session(Some(&summary("no-trade but risk-idle OK")), None).unwrap();
        assert!(matches!(v, Verdict::Refused(ref s) if s == "NO-GO"));
        assert_eq!(art["verdict"], "NO-GO");
    }

    #[test]
    fn missing_and_live_fail_close() {
        assert_eq!(
            map_paper_session(None, None).unwrap_err().code,
            "galahad_session_missing"
        );
        let mut live = summary("ok");
        live["mode"] = json!("live");
        assert_eq!(
            map_paper_session(Some(&live), None).unwrap_err().code,
            "galahad_live_refused"
        );
        let mut bare = summary("ok");
        bare.as_object_mut().unwrap().remove("run_id");
        assert_eq!(
            map_paper_session(Some(&bare), None).unwrap_err().code,
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
    fn engine_identity_mismatch_fails_closed() {
        let s = stage_with(Some("nautilus"));
        let summary = json!({"engine": "paper", "run_id": "x"});
        let err = check_engine_identity(&s, &summary).unwrap_err();
        assert_eq!(err.code, "galahad_engine_mismatch");
    }

    #[test]
    fn engine_identity_accepts_legacy_summaries() {
        let s = stage_with(Some("paper"));
        check_engine_identity(&s, &json!({"run_id": "x"})).unwrap();
        let s2 = stage_with(Some("nautilus"));
        check_engine_identity(&s2, &json!({"engine": "nautilus"})).unwrap();
    }
}
