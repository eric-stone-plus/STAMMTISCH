//! Shipped HIGHBALL adapter: `build-action-packet` over two typed upstream
//! artifacts plus the upstream QUINTE review product. The route request,
//! residual trace, and review result summary must be declared in the stage
//! input set and are resolved only through their event-pinned content
//! digests. The digest-verified `review.result` pins the QUINTE run_id; the
//! adapter then attaches the durable run product
//! (`<QUINTE_HOME>/runs/<run_id>/result.json`) and the exact QUINTE binary
//! that produced it (HIGHBALL verifies its digest against the run manifest
//! and re-inspects the run through it). Those two are the only ambient
//! reads; everything else stays digest-bound.

use std::cell::RefCell;
use std::path::PathBuf;

use serde_json::{json, Value};

use super::{Adapter, Collected, InvocationHandle, PollState, StageContext, Verdict};
use crate::canon;
use crate::error::AppError;
use crate::pipeline::Stage;

pub struct HighballAdapter {
    packet_script: PathBuf,
    packet: RefCell<Option<Value>>,
}

pub const ROUTE_REQUEST_INPUT: &str = "highball.route-request.json";
pub const RESIDUAL_TRACE_INPUT: &str = "highball.residual-trace.json";
pub const REVIEW_RESULT_INPUT: &str = "review.result";

fn resolve_highball_bin() -> Result<PathBuf, AppError> {
    if let Ok(raw) = std::env::var("HIGHBALL_BIN") {
        let path = PathBuf::from(raw);
        if path.is_file() {
            return Ok(path);
        }
        return Err(AppError::product(
            "product_cli_missing",
            format!("HIGHBALL_BIN={} is not a file", path.display()),
        ));
    }
    if let Ok(home) = std::env::var("HIGHBALL_HOME") {
        let home = PathBuf::from(home);
        for rel in [
            "target/debug/highball",
            "target/release/highball",
            "bin/highball",
            "target/debug/build-action-packet",
            "target/release/build-action-packet",
        ] {
            let cand = home.join(rel);
            if cand.is_file() {
                return Ok(cand);
            }
        }
        return Err(AppError::product(
            "product_cli_missing",
            format!(
                "HIGHBALL_HOME={} has no shipped highball binary (cargo build in HIGHBALL)",
                home.display()
            ),
        ));
    }
    super::cli::resolve_bin("HIGHBALL_BIN", "highball")
}

impl HighballAdapter {
    pub fn new(_stage: &Stage) -> Result<Self, AppError> {
        let packet_script = resolve_highball_bin()?;
        Ok(Self {
            packet_script,
            packet: RefCell::new(None),
        })
    }
}

/// Map an Action Packet 2.0 object. Missing `action_decision` fail-closes.
pub fn map_action_packet(packet: Option<&Value>) -> Result<Verdict, AppError> {
    let packet = packet.ok_or_else(missing_packet)?;
    if packet.get("packet_version").and_then(Value::as_str) != Some("2.0") {
        return Err(AppError::product(
            "highball_packet_invalid",
            format!(
                "HIGHBALL packet_version is {:?}, expected 2.0",
                packet.get("packet_version")
            ),
        ));
    }
    match packet.get("action_decision").and_then(Value::as_str) {
        Some("pass") => Ok(Verdict::Proceed),
        Some(d @ ("review" | "block")) => Ok(Verdict::Refused(d.to_string())),
        Some(other) => Err(AppError::product(
            "highball_packet_invalid",
            format!("HIGHBALL action_decision '{other}' is not pass/review/block"),
        )),
        None => Err(missing_packet()),
    }
}

pub fn missing_packet() -> AppError {
    AppError::product(
        "highball_packet_missing",
        "HIGHBALL produced no Action Packet 2.0; absence of a packet is fail-closed",
    )
}

fn receipt(packet: &Value) -> Value {
    let action = packet
        .get("action_decision")
        .and_then(Value::as_str)
        .unwrap_or("block");
    let decision = if action == "pass" {
        "AUTHORIZED"
    } else {
        "DENIED"
    };
    let route = packet
        .pointer("/route_decision/route")
        .and_then(Value::as_str)
        .unwrap_or("block");
    let route = match route {
        "direct-evidence" => "direct-evidence",
        "human-review" => "human-review",
        "block" => "block",
        // An unroutable class never auto-authorizes: fail closed.
        _ => "block",
    };
    json!({
        "schema": "highball.action-packet.v1",
        "packet_id": packet.get("packet_id").and_then(Value::as_str).unwrap_or("highball-packet"),
        "route": route,
        "decision": decision,
        "action_decision": action,
        "packet_sha256": canon::sha256_value_prefixed(packet),
        "reasons": packet.get("decision_reasons").cloned().unwrap_or_else(|| json!([])),
    })
}

fn declared_input_path(ctx: &StageContext, name: &str) -> Result<PathBuf, AppError> {
    let digest = ctx.inputs.get(name).ok_or_else(|| {
        AppError::integrity(
            "highball_input_missing",
            format!(
                "stage '{}' must declare typed input '{name}' from an upstream stage",
                ctx.stage.id
            ),
        )
    })?;
    let hex = digest.strip_prefix("sha256:").filter(|hex| {
        hex.len() == 64
            && hex
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    });
    let hex = hex.ok_or_else(|| {
        AppError::integrity(
            "highball_input_digest_invalid",
            format!("typed input '{name}' has malformed digest '{digest}'"),
        )
    })?;
    let path = ctx.run_dir.join("artifacts").join(hex);
    let bytes = std::fs::read(&path).map_err(|e| {
        AppError::integrity(
            "highball_input_missing",
            format!("typed input '{name}' is unreadable: {e}"),
        )
    })?;
    let actual = canon::sha256_prefixed(&bytes);
    if actual != *digest {
        return Err(AppError::integrity(
            "highball_input_digest_drift",
            format!("typed input '{name}' hashes to {actual}, expected {digest}"),
        ));
    }
    serde_json::from_slice::<Value>(&bytes).map_err(|e| {
        AppError::integrity(
            "highball_input_unparseable",
            format!("typed input '{name}' is not JSON: {e}"),
        )
    })?;
    Ok(path)
}

/// The QUINTE review product binding HIGHBALL's verify_cli path requires:
/// the durable run result, the state root it lives under, and the exact
/// binary that produced the run (HIGHBALL checks its sha256 against the run
/// manifest's runtime_sha256 and re-inspects the run through it).
#[derive(Debug)]
pub struct QuinteBinding {
    pub result: PathBuf,
    pub state_root: PathBuf,
    pub binary: PathBuf,
}

fn home_dir() -> Result<PathBuf, AppError> {
    std::env::var_os("HOME").map(PathBuf::from).ok_or_else(|| {
        AppError::product(
            "highball_quinte_home_missing",
            "HOME is unset; cannot resolve the default QUINTE state root (~/.quinte)",
        )
    })
}

/// The digest-verified review.result summary pins the QUINTE run_id; the
/// durable run product lives under the trusted runs root. QUINTE_HOME, when
/// set, must be absolute (HIGHBALL rejects a relative root).
fn quinte_state_root() -> Result<PathBuf, AppError> {
    match std::env::var("QUINTE_HOME") {
        Ok(raw) if raw.is_empty() => Err(AppError::product(
            "highball_quinte_home_invalid",
            "QUINTE_HOME is empty; it must pin an absolute QUINTE state root",
        )),
        Ok(raw) => {
            let root = PathBuf::from(raw);
            if !root.is_absolute() {
                return Err(AppError::product(
                    "highball_quinte_home_invalid",
                    format!("QUINTE_HOME={} is not an absolute path", root.display()),
                ));
            }
            Ok(root)
        }
        Err(_) => Ok(home_dir()?.join(".quinte")),
    }
}

/// The pinned QUINTE binary must be the exact one that produced the run:
/// HIGHBALL verifies its digest against the run manifest downstream.
fn quinte_binary() -> Result<PathBuf, AppError> {
    match std::env::var("HIGHBALL_QUINTE_BIN") {
        Ok(raw) if raw.is_empty() => Err(AppError::product(
            "highball_quinte_bin_invalid",
            "HIGHBALL_QUINTE_BIN is empty; it must name an absolute QUINTE executable",
        )),
        Ok(raw) => {
            let bin = PathBuf::from(raw);
            if !bin.is_absolute() {
                return Err(AppError::product(
                    "highball_quinte_bin_invalid",
                    format!("HIGHBALL_QUINTE_BIN={} is not absolute", bin.display()),
                ));
            }
            if !bin.is_file() {
                return Err(AppError::product(
                    "highball_quinte_bin_missing",
                    format!("HIGHBALL_QUINTE_BIN={} is not a file", bin.display()),
                ));
            }
            Ok(bin)
        }
        Err(_) => {
            let bin = home_dir()?.join(".cargo").join("bin").join("quinte");
            if !bin.is_file() {
                return Err(AppError::product(
                    "highball_quinte_bin_missing",
                    format!(
                        "no pinned QUINTE binary at {} (set HIGHBALL_QUINTE_BIN); the binary \
                         must be the exact one that produced the run — HIGHBALL verifies its \
                         digest against the run manifest's runtime_sha256",
                        bin.display()
                    ),
                ));
            }
            Ok(bin)
        }
    }
}

fn resolve_quinte_binding(ctx: &StageContext) -> Result<QuinteBinding, AppError> {
    let summary_path = declared_input_path(ctx, REVIEW_RESULT_INPUT)?;
    let bytes = std::fs::read(&summary_path).map_err(|e| {
        AppError::integrity(
            "highball_input_missing",
            format!("typed input '{REVIEW_RESULT_INPUT}' is unreadable: {e}"),
        )
    })?;
    let summary: Value = serde_json::from_slice(&bytes).map_err(|e| {
        AppError::integrity(
            "highball_input_unparseable",
            format!("typed input '{REVIEW_RESULT_INPUT}' is not JSON: {e}"),
        )
    })?;
    let run_id = summary
        .get("run_id")
        .and_then(Value::as_str)
        .filter(|id| !id.trim().is_empty())
        .ok_or_else(|| {
            AppError::product(
                "highball_quinte_run_missing",
                format!(
                    "typed input '{REVIEW_RESULT_INPUT}' carries no non-empty run_id; the \
                     deliver stage cannot bind the QUINTE run product without it"
                ),
            )
        })?;
    let state_root = quinte_state_root()?;
    // HIGHBALL canonicalizes the trusted runs root from the child QUINTE_HOME
    // and rejects a result outside it, so resolve through canonical paths.
    let state_root = std::fs::canonicalize(&state_root).unwrap_or(state_root);
    let result = state_root.join("runs").join(run_id).join("result.json");
    if !result.is_file() {
        return Err(AppError::product(
            "highball_quinte_result_missing",
            format!(
                "the QUINTE run product pinned by '{REVIEW_RESULT_INPUT}' (run_id {run_id}) \
                 does not exist: {}",
                result.display()
            ),
        ));
    }
    let result = std::fs::canonicalize(&result).unwrap_or(result);
    let binary = quinte_binary()?;
    Ok(QuinteBinding {
        result,
        state_root,
        binary,
    })
}

fn materialize_inputs(ctx: &StageContext) -> Result<(PathBuf, PathBuf, QuinteBinding), AppError> {
    Ok((
        declared_input_path(ctx, ROUTE_REQUEST_INPUT)?,
        declared_input_path(ctx, RESIDUAL_TRACE_INPUT)?,
        resolve_quinte_binding(ctx)?,
    ))
}

impl Adapter for HighballAdapter {
    fn preflight(&self, ctx: &StageContext) -> Result<(), AppError> {
        if !self.packet_script.is_file() {
            return Err(AppError::product(
                "adapter_preflight_failed",
                format!(
                    "HIGHBALL build-action-packet {} is not a file",
                    self.packet_script.display()
                ),
            ));
        }
        materialize_inputs(ctx)?;
        Ok(())
    }

    fn invoke(&self, ctx: &StageContext) -> Result<InvocationHandle, AppError> {
        let (request, trace, quinte) = materialize_inputs(ctx)?;
        let request_s = request.to_str().unwrap_or("");
        let trace_s = trace.to_str().unwrap_or("");
        let result_s = quinte.result.to_str().ok_or_else(|| {
            AppError::product(
                "highball_quinte_result_missing",
                format!("QUINTE result path is not UTF-8: {}", quinte.result.display()),
            )
        })?;
        let mut args: Vec<&str> =
            if self.packet_script.file_name().and_then(|s| s.to_str()) == Some("highball") {
                vec!["build-action-packet", request_s, trace_s]
            } else {
                vec![request_s, trace_s]
            };
        args.push("--quinte-result");
        args.push(result_s);
        let root_s = quinte.state_root.to_str().ok_or_else(|| {
            AppError::product(
                "highball_quinte_home_invalid",
                format!(
                    "QUINTE state root is not UTF-8: {}",
                    quinte.state_root.display()
                ),
            )
        })?;
        let bin_s = quinte.binary.to_str().ok_or_else(|| {
            AppError::product(
                "highball_quinte_bin_invalid",
                format!(
                    "QUINTE binary path is not UTF-8: {}",
                    quinte.binary.display()
                ),
            )
        })?;
        let (_code, packet) = super::cli::run_json_env(
            &self.packet_script,
            &args,
            &[("QUINTE_HOME", root_s), ("HIGHBALL_QUINTE_BIN", bin_s)],
        )?;
        map_action_packet(Some(&packet))?;
        *self.packet.borrow_mut() = Some(packet);
        Ok(InvocationHandle {
            id: format!("highball-packet-{}", &ctx.run_id[..8.min(ctx.run_id.len())]),
            product: "highball".into(),
        })
    }

    fn poll(&self, _h: &InvocationHandle) -> PollState {
        if self.packet.borrow().is_some() {
            PollState::Completed
        } else {
            PollState::Failed("HIGHBALL produced no packet".into())
        }
    }

    fn collect(&self, _h: &InvocationHandle, _ctx: &StageContext) -> Result<Collected, AppError> {
        let packet = self.packet.borrow().clone().ok_or_else(missing_packet)?;
        let verdict = map_action_packet(Some(&packet))?;
        Ok(Collected {
            receipts: vec![receipt(&packet)],
            artifacts: vec![("deliver.packet.json".into(), packet)],
            verdict,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn packet(decision: &str) -> Value {
        json!({
            "packet_version": "2.0",
            "action_decision": decision,
            "route_decision": {"route": "direct-evidence"},
            "decision_reasons": ["ok"]
        })
    }

    #[test]
    fn map_pass_proceeds_review_and_block_refuse() {
        assert!(matches!(
            map_action_packet(Some(&packet("pass"))).unwrap(),
            Verdict::Proceed
        ));
        assert!(matches!(
            map_action_packet(Some(&packet("block"))).unwrap(),
            Verdict::Refused(ref v) if v == "block"
        ));
        assert!(matches!(
            map_action_packet(Some(&packet("review"))).unwrap(),
            Verdict::Refused(ref v) if v == "review"
        ));
    }

    #[test]
    fn missing_packet_fail_closes() {
        let err = map_action_packet(None).unwrap_err();
        assert_eq!(err.code, "highball_packet_missing");
        let mut bare = packet("pass");
        bare.as_object_mut().unwrap().remove("action_decision");
        assert_eq!(
            map_action_packet(Some(&bare)).unwrap_err().code,
            "highball_packet_missing"
        );
    }

    // -------------------------------------------- QUINTE product binding

    /// QUINTE_HOME / HIGHBALL_QUINTE_BIN are process-global, so tests that
    /// touch them are serialized by a mutex and restore on drop.
    struct EnvGuard {
        _lock: std::sync::MutexGuard<'static, ()>,
        saved: Vec<(&'static str, Option<std::ffi::OsString>)>,
    }

    static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    impl EnvGuard {
        fn set(vars: &[(&'static str, Option<&std::path::Path>)]) -> Self {
            let lock = ENV_LOCK.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            let mut saved = Vec::new();
            for (key, value) in vars {
                saved.push((*key, std::env::var_os(key)));
                match value {
                    Some(v) => std::env::set_var(key, v),
                    None => std::env::remove_var(key),
                }
            }
            EnvGuard { _lock: lock, saved }
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            for (key, old) in self.saved.drain(..) {
                match old {
                    Some(v) => std::env::set_var(key, v),
                    None => std::env::remove_var(key),
                }
            }
        }
    }

    struct DeliverFixture {
        dir: PathBuf,
        run_dir: PathBuf,
        quinte_home: PathBuf,
        quinte_bin: PathBuf,
        stage: crate::pipeline::Stage,
        doctrine: crate::doctrine::DoctrinePack,
        inputs: std::collections::BTreeMap<String, String>,
    }

    /// A run directory with the declared carriers content-addressed under
    /// `artifacts/`, a QUINTE state root, and a pinned binary. The
    /// review.result summary is written from `summary`; the durable run
    /// product is present only when `durable` is true.
    fn deliver_fixture(summary: Value, run_id: &str, durable: bool) -> DeliverFixture {
        let dir = std::env::temp_dir().join(format!(
            "stammtisch-highball-{}",
            crate::ids::uuid_v7().unwrap()
        ));
        let run_dir = dir.join("run");
        let artifacts = run_dir.join("artifacts");
        std::fs::create_dir_all(&artifacts).unwrap();
        let mut inputs = std::collections::BTreeMap::new();
        for (name, value) in [
            (ROUTE_REQUEST_INPUT, json!({"request_version": "2.0"})),
            (RESIDUAL_TRACE_INPUT, json!({"trace_version": "1.0"})),
            (REVIEW_RESULT_INPUT, summary),
        ] {
            let bytes = serde_json::to_vec(&value).unwrap();
            let digest = canon::sha256_prefixed(&bytes);
            let hex = digest.strip_prefix("sha256:").unwrap();
            std::fs::write(artifacts.join(hex), &bytes).unwrap();
            inputs.insert(name.to_string(), digest);
        }
        let quinte_home = dir.join("quinte-home");
        if durable {
            let run = quinte_home.join("runs").join(run_id);
            std::fs::create_dir_all(&run).unwrap();
            std::fs::write(run.join("result.json"), b"{}").unwrap();
        } else {
            std::fs::create_dir_all(&quinte_home).unwrap();
        }
        let quinte_bin = dir.join("quinte-bin");
        std::fs::write(&quinte_bin, b"#!/bin/sh\n").unwrap();
        std::fs::write(
            dir.join("doctrine.json"),
            r#"{"pack":"galahad","version":"0.1.0"}"#,
        )
        .unwrap();
        std::fs::write(dir.join("gates.json"), r#"{"gates":[]}"#).unwrap();
        let doctrine = crate::doctrine::load_dir(&dir).unwrap();
        let stage = crate::pipeline::validate(
            &json!({
                "schema": "stammtisch.pipeline.v0", "id": "t-pipe",
                "doctrine": {"pack": "galahad"},
                "stages": [{"id": "deliver", "product": "highball", "adapter": "fake"}]
            }),
            std::path::Path::new("x.json"),
        )
        .unwrap()
        .stages
        .into_iter()
        .next()
        .unwrap();
        DeliverFixture {
            dir,
            run_dir,
            quinte_home,
            quinte_bin,
            stage,
            doctrine,
            inputs,
        }
    }

    impl DeliverFixture {
        fn ctx(&self) -> StageContext<'_> {
            StageContext {
                run_id: "t-run",
                pipeline_id: "t-pipe",
                stage: &self.stage,
                doctrine: &self.doctrine,
                inputs: &self.inputs,
                run_dir: &self.run_dir,
            }
        }
    }

    impl Drop for DeliverFixture {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.dir).ok();
        }
    }

    #[test]
    fn quinte_binding_resolves_through_the_digest_pinned_run_id() {
        let run_id = crate::ids::uuid_v7().unwrap();
        let fixture = deliver_fixture(json!({"run_id": run_id}), &run_id, true);
        let _env = EnvGuard::set(&[
            ("QUINTE_HOME", Some(&fixture.quinte_home)),
            ("HIGHBALL_QUINTE_BIN", Some(&fixture.quinte_bin)),
        ]);
        let binding = resolve_quinte_binding(&fixture.ctx()).unwrap();
        let expected = std::fs::canonicalize(
            fixture
                .quinte_home
                .join("runs")
                .join(&run_id)
                .join("result.json"),
        )
        .unwrap();
        assert_eq!(binding.result, expected);
        assert_eq!(
            binding.state_root,
            std::fs::canonicalize(&fixture.quinte_home).unwrap()
        );
        assert_eq!(binding.binary, fixture.quinte_bin);
    }

    #[test]
    fn quinte_binding_fail_closes_without_run_id() {
        let run_id = crate::ids::uuid_v7().unwrap();
        let fixture = deliver_fixture(json!({"status": "completed"}), &run_id, true);
        let _env = EnvGuard::set(&[
            ("QUINTE_HOME", Some(&fixture.quinte_home)),
            ("HIGHBALL_QUINTE_BIN", Some(&fixture.quinte_bin)),
        ]);
        for summary in [json!({"status": "completed"}), json!({"run_id": "  "})] {
            let fixture = deliver_fixture(summary, &run_id, true);
            let err = resolve_quinte_binding(&fixture.ctx()).unwrap_err();
            assert_eq!(err.code, "highball_quinte_run_missing");
        }
    }

    #[test]
    fn quinte_binding_fail_closes_when_the_durable_result_is_absent() {
        let run_id = crate::ids::uuid_v7().unwrap();
        let fixture = deliver_fixture(json!({"run_id": run_id}), &run_id, false);
        let _env = EnvGuard::set(&[
            ("QUINTE_HOME", Some(&fixture.quinte_home)),
            ("HIGHBALL_QUINTE_BIN", Some(&fixture.quinte_bin)),
        ]);
        let err = resolve_quinte_binding(&fixture.ctx()).unwrap_err();
        assert_eq!(err.code, "highball_quinte_result_missing");
        assert!(err.message.contains(&run_id), "error must name the run");
    }

    #[test]
    fn quinte_binding_fail_closes_without_the_pinned_binary() {
        let run_id = crate::ids::uuid_v7().unwrap();
        let fixture = deliver_fixture(json!({"run_id": run_id}), &run_id, true);
        let missing = fixture.dir.join("no-such-quinte");
        let _env = EnvGuard::set(&[
            ("QUINTE_HOME", Some(&fixture.quinte_home)),
            ("HIGHBALL_QUINTE_BIN", Some(&missing)),
        ]);
        let err = resolve_quinte_binding(&fixture.ctx()).unwrap_err();
        assert_eq!(err.code, "highball_quinte_bin_missing");
    }

    #[test]
    fn quinte_binding_rejects_a_relative_quinte_home() {
        let run_id = crate::ids::uuid_v7().unwrap();
        let fixture = deliver_fixture(json!({"run_id": run_id}), &run_id, true);
        let _env = EnvGuard::set(&[
            ("QUINTE_HOME", Some(std::path::Path::new("relative/quinte"))),
            ("HIGHBALL_QUINTE_BIN", Some(&fixture.quinte_bin)),
        ]);
        let err = resolve_quinte_binding(&fixture.ctx()).unwrap_err();
        assert_eq!(err.code, "highball_quinte_home_invalid");
    }
}
