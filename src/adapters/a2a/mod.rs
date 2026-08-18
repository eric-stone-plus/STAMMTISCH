//! Real A2A v1.0 product adapter (docs/protocol-layer.md). One adapter
//! instance per stage; drives any A2A-speaking agent through the
//! preflight/invoke/poll/collect contract:
//!
//! ```text
//! preflight  GET Agent Card → interface negotiation (JSONRPC, 1.0) →
//!            digest pinning (a stage-configured pre-pin must match)
//! invoke     SendMessage (returnImmediately) → task id
//! poll       GetTask loop, sleeping poll_seconds between one-shot
//!            observations, bounded by timeout_seconds
//! collect    terminal GetTask → artifacts mapped to the stage outputs
//! ```
//!
//! Every new wire observation is wrapped into an `a2a.invocation.v2` receipt
//! (endpoint + card digest + task id + task state + verbatim upstream
//! payload pinned by digest), so failure paths keep their evidence via
//! [`Adapter::drain_receipts`]. The runner never cancels a product: a
//! timed-out or input-requiring task is left running and the halt detail
//! reports the task id and endpoint for operator handoff.

use std::cell::RefCell;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use super::{Adapter, Collected, InvocationHandle, PollState, StageContext, Verdict};
use crate::canon;
use crate::error::AppError;
use crate::pipeline::Runtime;

pub mod client;
pub mod model;

use model::{AgentCard, Artifact, Message, Part, Task, TaskState};

/// Canonical A2A v1.0 well-known discovery path, appended to the endpoint
/// origin when the stage config does not pin a `card_url`.
pub const WELL_KNOWN_CARD_PATH: &str = "/.well-known/agent-card.json";

/// The binding this stage's invocation is pinned to, established once at
/// preflight and recorded in every receipt.
struct CardBinding {
    card: Value,
    card_digest: String,
    card_url: String,
    agent: String,
    protocol_version: String,
}

pub struct A2aAdapter {
    runtime: Runtime,
    client: client::A2aClient,
    card: RefCell<Option<CardBinding>>,
    receipts: RefCell<Vec<Value>>,
    /// (stage_id, timeout_seconds, poll_seconds), pinned at invoke.
    stage: RefCell<Option<(String, u64, u64)>>,
    /// The run context id sent with SendMessage. Every returned task must
    /// preserve it; otherwise a response from another invocation could be
    /// accepted under this stage's evidence chain.
    context_id: RefCell<Option<String>>,
}

impl A2aAdapter {
    pub fn new(runtime: &Runtime) -> Result<Self, AppError> {
        validate_endpoint(&runtime.endpoint)?;
        let client = client::A2aClient::new(&runtime.endpoint, runtime.token_env.as_deref())?;
        Ok(Self {
            runtime: runtime.clone(),
            client,
            card: RefCell::new(None),
            receipts: RefCell::new(Vec::new()),
            stage: RefCell::new(None),
            context_id: RefCell::new(None),
        })
    }

    fn binding(&self) -> Result<(), AppError> {
        if self.card.borrow().is_none() {
            return Err(AppError::internal("a2a operation before preflight"));
        }
        Ok(())
    }

    /// Wrap one wire observation into its receipt revision. `upstream` is
    /// the verbatim JSON-RPC result object; the envelope binds it by
    /// digest so later verification re-checks the observed bytes.
    fn receipt(
        &self,
        operation: &str,
        upstream: &Value,
        task_id: Option<&str>,
        task_state: Option<TaskState>,
    ) -> Result<Value, AppError> {
        self.binding()?;
        let (stage_id, _, _) = self
            .stage
            .borrow()
            .clone()
            .ok_or_else(|| AppError::internal("a2a receipt before invoke"))?;
        let card = self.card.borrow();
        let b = card.as_ref().expect("binding checked");
        let mut r = json!({
            "schema": "a2a.invocation.v2",
            "host": {
                "endpoint": self.runtime.endpoint,
                "card_url": b.card_url,
                "card_sha256": b.card_digest,
                "agent": b.agent,
                "protocol_version": b.protocol_version,
            },
            "stage": stage_id,
            "operation": operation,
            "observed_at": crate::time::now_rfc3339(),
            "invocation_id": crate::ids::uuid_v7()?,
            "upstream": upstream,
            "upstream_sha256": canon::sha256_value_prefixed(upstream),
        });
        if let Some(t) = task_id {
            r["task_id"] = json!(t);
        }
        if let Some(s) = task_state {
            r["task_state"] = json!(s.as_str());
            r["context_id"] = json!(self.context_id.borrow().clone().ok_or_else(|| {
                AppError::internal("a2a task receipt before invocation context was pinned")
            })?);
        }
        Ok(r)
    }

    fn push_receipt(&self, receipt: Value) {
        self.receipts.borrow_mut().push(receipt);
    }

    fn validate_task_binding(
        &self,
        task: &Task,
        expected_task_id: Option<&str>,
    ) -> Result<(), AppError> {
        if let Some(expected) = expected_task_id {
            if task.id != expected {
                return Err(AppError::integrity(
                    "a2a_task_mismatch",
                    format!(
                        "A2A response task id '{}' does not match invoked task '{expected}'",
                        task.id
                    ),
                ));
            }
        }
        let expected_context = self.context_id.borrow();
        let expected_context = expected_context.as_deref().ok_or_else(|| {
            AppError::internal("a2a task observed before invocation context was pinned")
        })?;
        if task.context_id.as_deref() != Some(expected_context) {
            return Err(AppError::integrity(
                "a2a_context_mismatch",
                format!(
                    "A2A task '{}' context {:?} does not match run context '{}'",
                    task.id, task.context_id, expected_context
                ),
            ));
        }
        Ok(())
    }
}

impl Adapter for A2aAdapter {
    fn preflight(&self, ctx: &StageContext) -> Result<(), AppError> {
        let card_url = self
            .runtime
            .card_url
            .clone()
            .unwrap_or_else(|| default_card_url(&self.runtime.endpoint));
        let card = self.client.discover(&card_url)?;

        // Interface negotiation: the agent must offer the JSONRPC binding
        // at exactly the protocol version this adapter speaks.
        let typed: AgentCard = serde_json::from_value(card.clone()).map_err(|e| {
            AppError::integrity(
                "a2a_wire_invalid",
                format!("agent card at {card_url} violates the A2A v1.0 model: {e}"),
            )
        })?;
        let iface = typed
            .supported_interfaces
            .iter()
            .find(|i| i.protocol_binding == "JSONRPC")
            .ok_or_else(|| {
                AppError::integrity(
                    "a2a_protocol_mismatch",
                    format!("agent card at {card_url} offers no JSONRPC interface"),
                )
            })?;
        if iface.protocol_version != client::PROTOCOL_VERSION {
            return Err(AppError::integrity(
                "a2a_protocol_mismatch",
                format!(
                    "agent card at {card_url} declares JSONRPC protocolVersion '{}'; \
                     this adapter negotiates '{}' only",
                    iface.protocol_version,
                    client::PROTOCOL_VERSION
                ),
            ));
        }

        let card_digest = canon::sha256_value_prefixed(&card);
        if let Some(pin) = &self.runtime.card_sha256 {
            if pin != &card_digest {
                return Err(AppError::integrity(
                    "a2a_card_pin_mismatch",
                    format!(
                        "stage pins agent card {pin} but the card at {card_url} \
                         digests to {card_digest}"
                    ),
                ));
            }
        }

        *self.card.borrow_mut() = Some(CardBinding {
            agent: typed.name.clone(),
            protocol_version: iface.protocol_version.clone(),
            card_digest: card_digest.clone(),
            card_url: card_url.clone(),
            card,
        });
        *self.stage.borrow_mut() = Some((
            ctx.stage.id.clone(),
            ctx.stage.timeout_seconds,
            ctx.stage.poll_seconds,
        ));

        let receipt = self.receipt("card_discovery", &self.card_value()?, None, None)?;
        self.push_receipt(receipt);
        Ok(())
    }

    fn invoke(&self, ctx: &StageContext) -> Result<InvocationHandle, AppError> {
        self.binding()?;
        *self.context_id.borrow_mut() = Some(ctx.run_id.to_string());

        let mut parts = vec![Part {
            text: Some(format!(
                "STAMMTISCH pipeline '{}' run '{}' stage '{}': review the attached \
                 evidence artifact(s) and return the product's contract artifacts.",
                ctx.pipeline_id, ctx.run_id, ctx.stage.id
            )),
            ..Default::default()
        }];
        for (name, digest) in ctx.inputs {
            let hex = digest
                .strip_prefix("sha256:")
                .ok_or_else(|| AppError::internal("input digest not sha256-prefixed"))?;
            let path = ctx.run_dir.join("artifacts").join(hex);
            let bytes = std::fs::read(&path).map_err(|e| {
                AppError::integrity(
                    "a2a_input_missing",
                    format!("input artifact '{name}' unreadable: {e}"),
                )
            })?;
            let value: Value = serde_json::from_slice(&bytes).map_err(|e| {
                AppError::integrity(
                    "a2a_input_unparseable",
                    format!("input artifact '{name}' is not JSON: {e}"),
                )
            })?;
            parts.push(Part {
                data: Some(value),
                filename: Some(name.clone()),
                media_type: Some("application/json".to_string()),
                ..Default::default()
            });
        }

        let message = Message {
            message_id: crate::ids::uuid_v7()?,
            context_id: Some(ctx.run_id.to_string()),
            task_id: None,
            role: "ROLE_USER".to_string(),
            parts,
            metadata: Some(json!({
                "pipeline": ctx.pipeline_id,
                "stage": ctx.stage.id,
            })),
        };
        let configuration = json!({
            "acceptedOutputModes": ["application/json"],
            "historyLength": 10,
            "returnImmediately": true,
        });
        let (raw, resp) = self.client.send_message(&message, configuration)?;
        let task = resp.task.ok_or_else(|| {
            AppError::integrity(
                "a2a_wire_invalid",
                "agent replied with a direct message instead of accepting the task; \
                 a stage invocation needs a pollable task",
            )
        })?;
        self.validate_task_binding(&task, None)?;

        self.push_receipt(self.receipt(
            "send_message",
            &raw,
            Some(&task.id),
            Some(task.status.state),
        )?);
        Ok(InvocationHandle {
            id: task.id.clone(),
            product: ctx.stage.product.clone(),
        })
    }

    fn poll(&self, handle: &InvocationHandle) -> PollState {
        let (_, timeout_seconds, poll_seconds) = match self.stage.borrow().clone() {
            Some(s) => s,
            None => return PollState::Halted("internal: poll without invoke".into()),
        };
        let started = Instant::now();
        let deadline = started + Duration::from_secs(timeout_seconds);
        loop {
            let now = Instant::now();
            if now >= deadline {
                return PollState::Halted(format!(
                    "task {} at {} did not reach a terminal state within {}s; NOT cancelled — operator handoff",
                    handle.id, self.runtime.endpoint, timeout_seconds
                ));
            }
            let remaining = deadline.saturating_duration_since(now);
            let (raw, task) = match self.client.get_task_with_timeout(&handle.id, remaining) {
                Ok(v) => v,
                Err(e) => {
                    return PollState::Halted(format!(
                        "poll of task {} at {} failed: {} ({})",
                        handle.id, self.runtime.endpoint, e.message, e.code
                    ))
                }
            };
            if let Err(e) = self.validate_task_binding(&task, Some(&handle.id)) {
                return PollState::Halted(format!("{} ({})", e.message, e.code));
            }
            match self.receipt("get_task", &raw, Some(&task.id), Some(task.status.state)) {
                Ok(r) => self.push_receipt(r),
                Err(e) => return PollState::Halted(format!("failed to record wire receipt: {e}")),
            }
            match task.status.state {
                TaskState::Submitted | TaskState::Working => {}
                TaskState::Completed => return PollState::Completed,
                TaskState::Failed | TaskState::Canceled | TaskState::Rejected => {
                    return PollState::Failed(format!(
                        "task {} at {} reached terminal state {}",
                        handle.id,
                        self.runtime.endpoint,
                        task.status.state.as_str()
                    ))
                }
                TaskState::InputRequired | TaskState::AuthRequired => {
                    return PollState::Halted(format!(
                        "task {} at {} requires input ({}); NOT cancelled — \
                         operator handoff",
                        handle.id,
                        self.runtime.endpoint,
                        task.status.state.as_str()
                    ))
                }
                TaskState::Unspecified => {
                    return PollState::Halted(format!(
                        "task {} at {} reported TASK_STATE_UNSPECIFIED — ambiguous; \
                         NOT cancelled — operator handoff",
                        handle.id, self.runtime.endpoint
                    ))
                }
            }
            if started.elapsed() >= Duration::from_secs(timeout_seconds) {
                return PollState::Halted(format!(
                    "task {} at {} did not reach a terminal state within {}s \
                     (last state {}); NOT cancelled — operator handoff",
                    handle.id,
                    self.runtime.endpoint,
                    timeout_seconds,
                    task.status.state.as_str()
                ));
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return PollState::Halted(format!(
                    "task {} at {} did not reach a terminal state within {}s; NOT cancelled — operator handoff",
                    handle.id, self.runtime.endpoint, timeout_seconds
                ));
            }
            std::thread::sleep(Duration::from_secs(poll_seconds).min(remaining));
        }
    }

    fn collect(
        &self,
        handle: &InvocationHandle,
        ctx: &StageContext,
    ) -> Result<Collected, AppError> {
        let (raw, task) = self.client.get_task(&handle.id)?;
        self.validate_task_binding(&task, Some(&handle.id))?;
        if task.status.state != TaskState::Completed {
            return Err(AppError::integrity(
                "a2a_handoff_invalid",
                format!(
                    "collect observed {} for task {}; only TASK_STATE_COMPLETED hands off",
                    task.status.state.as_str(),
                    handle.id
                ),
            ));
        }
        self.push_receipt(self.receipt(
            "get_task",
            &raw,
            Some(&task.id),
            Some(task.status.state),
        )?);

        let outputs = &ctx.stage.outputs;
        if task.artifacts.is_empty() {
            return Err(AppError::integrity(
                "a2a_artifacts_missing",
                format!(
                    "task {} completed with no artifacts; stage '{}' declares {} outputs",
                    handle.id,
                    ctx.stage.id,
                    outputs.len()
                ),
            ));
        }
        if task.artifacts.len() != outputs.len() {
            return Err(AppError::integrity(
                "a2a_artifacts_mismatch",
                format!(
                    "task {} returned {} artifacts but stage '{}' declares {} outputs \
                     {:?}; the artifact set is contractual, extras are ambiguous",
                    handle.id,
                    task.artifacts.len(),
                    ctx.stage.id,
                    outputs.len(),
                    outputs
                ),
            ));
        }
        let mut by_name = std::collections::BTreeMap::new();
        for artifact in &task.artifacts {
            let name = artifact.name.as_deref().ok_or_else(|| {
                AppError::integrity(
                    "a2a_artifact_name_missing",
                    format!(
                        "task {} returned artifact '{}' without a contract name",
                        handle.id, artifact.artifact_id
                    ),
                )
            })?;
            if !outputs.iter().any(|declared| declared == name) {
                return Err(AppError::integrity(
                    "a2a_artifact_name_mismatch",
                    format!(
                        "task {} returned undeclared artifact name '{}'; stage '{}' declares {:?}",
                        handle.id, name, ctx.stage.id, outputs
                    ),
                ));
            }
            if by_name
                .insert(name.to_string(), artifact_value(artifact)?)
                .is_some()
            {
                return Err(AppError::integrity(
                    "a2a_artifact_name_duplicate",
                    format!(
                        "task {} returned artifact name '{}' more than once",
                        handle.id, name
                    ),
                ));
            }
        }
        let mut artifacts = Vec::new();
        for name in outputs {
            let value = by_name.remove(name).ok_or_else(|| {
                AppError::integrity(
                    "a2a_artifact_name_missing",
                    format!(
                        "task {} did not return declared artifact name '{}'",
                        handle.id, name
                    ),
                )
            })?;
            artifacts.push((name.clone(), value));
        }

        let receipts = self.receipts.borrow_mut().drain(..).collect();
        Ok(Collected {
            receipts,
            artifacts,
            verdict: Verdict::Proceed,
        })
    }

    fn drain_receipts(&self) -> Vec<Value> {
        self.receipts.borrow_mut().drain(..).collect()
    }
}

impl A2aAdapter {
    fn card_value(&self) -> Result<Value, AppError> {
        Ok(self
            .card
            .borrow()
            .as_ref()
            .ok_or_else(|| AppError::internal("a2a receipt before preflight"))?
            .card
            .clone())
    }
}

/// Extract the JSON payload of an upstream artifact: the first part
/// carrying structured data, or text that parses as JSON. Anything else
/// fails closed — a completed task whose output cannot be read as the
/// stage's contract artifact halts, it is never skipped.
fn artifact_value(artifact: &Artifact) -> Result<Value, AppError> {
    for part in &artifact.parts {
        if let Some(data) = &part.data {
            return Ok(data.clone());
        }
        if let Some(text) = &part.text {
            if let Ok(v) = serde_json::from_str::<Value>(text) {
                return Ok(v);
            }
        }
    }
    Err(AppError::integrity(
        "a2a_artifact_unparseable",
        format!("artifact '{}' carries no JSON part", artifact.artifact_id),
    ))
}

fn validate_endpoint(endpoint: &str) -> Result<(), AppError> {
    match endpoint.split_once("://") {
        Some(("http", rest)) | Some(("https", rest)) => {
            if rest
                .split('/')
                .next()
                .map(|a| !a.is_empty())
                .unwrap_or(false)
            {
                Ok(())
            } else {
                Err(AppError::usage(
                    "a2a_config_invalid",
                    format!("runtime endpoint '{endpoint}' has an empty host"),
                ))
            }
        }
        _ => Err(AppError::usage(
            "a2a_config_invalid",
            format!("runtime endpoint '{endpoint}' must be an absolute http(s) URL"),
        )),
    }
}

/// `<endpoint origin>/.well-known/agent-card.json`.
fn default_card_url(endpoint: &str) -> String {
    let (scheme, rest) = endpoint.split_once("://").unwrap_or(("http", endpoint));
    let authority = rest.split('/').next().unwrap_or(rest);
    format!("{scheme}://{authority}{WELL_KNOWN_CARD_PATH}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_card_url_derives_from_endpoint_origin() {
        assert_eq!(
            default_card_url("https://agent.example.com:9900/rpc"),
            "https://agent.example.com:9900/.well-known/agent-card.json"
        );
        assert_eq!(
            default_card_url("http://127.0.0.1:8801/"),
            "http://127.0.0.1:8801/.well-known/agent-card.json"
        );
    }

    #[test]
    fn endpoint_must_be_absolute_http() {
        assert!(validate_endpoint("https://a.example.invalid/").is_ok());
        assert!(validate_endpoint("http://127.0.0.1:1").is_ok());
        assert!(validate_endpoint("/tmp/agent.sock").is_err());
        assert!(validate_endpoint("ftp://a.example.invalid/").is_err());
        assert!(validate_endpoint("http://").is_err());
    }
}
