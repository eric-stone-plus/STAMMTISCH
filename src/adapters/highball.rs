//! Shipped HIGHBALL adapter: `build-action-packet` over two typed upstream
//! artifacts. The route request and residual trace must be declared in the
//! stage input set and are resolved only through their event-pinned content
//! digests. Ambient files never participate in authorization evidence.

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

fn materialize_inputs(ctx: &StageContext) -> Result<(PathBuf, PathBuf), AppError> {
    Ok((
        declared_input_path(ctx, ROUTE_REQUEST_INPUT)?,
        declared_input_path(ctx, RESIDUAL_TRACE_INPUT)?,
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
        let (request, trace) = materialize_inputs(ctx)?;
        let request_s = request.to_str().unwrap_or("");
        let trace_s = trace.to_str().unwrap_or("");
        let args: Vec<&str> =
            if self.packet_script.file_name().and_then(|s| s.to_str()) == Some("highball") {
                vec!["build-action-packet", request_s, trace_s]
            } else {
                vec![request_s, trace_s]
            };
        let (_code, packet) = super::cli::run_json(&self.packet_script, &args)?;
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
}
