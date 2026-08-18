//! Minimal synchronous A2A v1.0 JSON-RPC client (docs/protocol-layer.md):
//! Agent Card discovery over GET plus the two JSON-RPC operations the
//! adapter's invoke/poll/collect lifecycle needs (SendMessage, GetTask).
//! Deliberately tiny: no streaming, no push notifications, no task
//! cancellation — the runner never cancels a product; a timed-out task is
//! left running and reported for operator handoff.
//!
//! Error discipline: transport/HTTP failures are product failures (the
//! product is unreachable); every wire ambiguity — a non-JSON-RPC
//! envelope, an id mismatch, an unparsable payload — is an integrity
//! error, which the runner turns into a halt.

use std::io::Read;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use serde_json::{json, Value};

use crate::error::AppError;

use super::model::{AgentCard, Message, RpcRequest, SendMessageResponse, Task};

/// Negotiated protocol version for the JSONRPC interface.
pub const PROTOCOL_VERSION: &str = "1.0";
/// Standard A2A service parameter header, sent on every request.
pub const A2A_VERSION_HEADER: &str = "A2A-Version";

pub struct A2aClient {
    endpoint: String,
    agent: ureq::Agent,
    token: Option<String>,
    next_id: AtomicU64,
}

impl A2aClient {
    /// Build the client. `token_env` names an environment variable holding
    /// the bearer token; a declared-but-unset variable is a hard config
    /// error (a resolved-but-broken config never silently degrades).
    pub fn new(endpoint: &str, token_env: Option<&str>) -> Result<Self, AppError> {
        let token = match token_env {
            Some(name) => match std::env::var(name) {
                Ok(t) if !t.is_empty() => Some(t),
                Ok(_) => {
                    return Err(AppError::usage(
                        "a2a_token_empty",
                        format!("token env var '{name}' is set but empty"),
                    ))
                }
                Err(_) => {
                    return Err(AppError::usage(
                        "a2a_token_missing",
                        format!("token env var '{name}' is not set"),
                    ))
                }
            },
            None => None,
        };
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(10))
            .timeout_read(Duration::from_secs(120))
            .build();
        Ok(Self {
            endpoint: endpoint.to_string(),
            agent,
            token,
            next_id: AtomicU64::new(1),
        })
    }

    /// Fetch the Agent Card. Wire-level only: returns the raw card document;
    /// interface negotiation (binding + version policy) is the adapter's.
    pub fn discover(&self, card_url: &str) -> Result<Value, AppError> {
        let body = self.get(card_url)?;
        let card: Value = serde_json::from_str(&body).map_err(|e| {
            AppError::integrity(
                "a2a_wire_invalid",
                format!("agent card at {card_url} is not JSON: {e}"),
            )
        })?;
        if !card.is_object() {
            return Err(AppError::integrity(
                "a2a_wire_invalid",
                format!("agent card at {card_url} is not a JSON object"),
            ));
        }
        // Typed check of the fields the adapter depends on; unknown fields
        // are tolerated (extension room) and pinned by digest in the
        // invocation receipt.
        serde_json::from_value::<AgentCard>(card.clone()).map_err(|e| {
            AppError::integrity(
                "a2a_wire_invalid",
                format!("agent card at {card_url} violates the A2A v1.0 model: {e}"),
            )
        })?;
        Ok(card)
    }

    /// SendMessage with `returnImmediately: true`; returns the raw wire
    /// result plus the typed oneof wrapper (exactly one of task/message).
    pub fn send_message(
        &self,
        message: &Message,
        configuration: Value,
    ) -> Result<(Value, SendMessageResponse), AppError> {
        let result = self.rpc(
            "SendMessage",
            json!({"message": message, "configuration": configuration}),
        )?;
        let resp: SendMessageResponse = serde_json::from_value(result.clone()).map_err(|e| {
            AppError::integrity(
                "a2a_wire_invalid",
                format!("SendMessage result violates the A2A v1.0 model: {e}"),
            )
        })?;
        match (&resp.task, &resp.message) {
            (Some(_), None) | (None, Some(_)) => Ok((result, resp)),
            _ => Err(AppError::integrity(
                "a2a_wire_invalid",
                "SendMessage response carries neither exactly one task nor message",
            )),
        }
    }

    /// GetTask snapshot; returns the raw wire result plus the typed task.
    pub fn get_task(&self, task_id: &str) -> Result<(Value, Task), AppError> {
        self.get_task_with_timeout(task_id, Duration::from_secs(120))
    }

    /// CancelTask (A2A v1.0). Best-effort by contract: a product may refuse
    /// or ignore cancellation. Used by `stammtisch reconcile` to release a
    /// remote task left running by an interrupted local run, so the next
    /// run is not rejected by the product's one-active discipline.
    pub fn cancel_task(&self, task_id: &str) -> Result<Value, AppError> {
        self.rpc_with_timeout(
            "CancelTask",
            json!({"id": task_id}),
            Some(Duration::from_secs(30)),
        )
    }

    /// Deadline-aware GetTask used by the poll loop. The request-level
    /// timeout covers connect, write and read and is clamped to the stage's
    /// remaining wall-clock budget.
    pub fn get_task_with_timeout(
        &self,
        task_id: &str,
        timeout: Duration,
    ) -> Result<(Value, Task), AppError> {
        let result = self.rpc_with_timeout("GetTask", json!({"id": task_id}), Some(timeout))?;
        let task: Task = serde_json::from_value(result.clone()).map_err(|e| {
            AppError::integrity(
                "a2a_wire_invalid",
                format!("GetTask result violates the A2A v1.0 model: {e}"),
            )
        })?;
        Ok((result, task))
    }

    // ------------------------------------------------------------ plumbing

    fn get(&self, url: &str) -> Result<String, AppError> {
        let request = self
            .agent
            .get(url)
            .set(A2A_VERSION_HEADER, PROTOCOL_VERSION)
            .set("Accept", "application/json");
        let request = self.apply_token(request);
        match request.call() {
            Ok(resp) => {
                let mut body = String::new();
                resp.into_reader()
                    .read_to_string(&mut body)
                    .map_err(|e| transport_error(url, &e))?;
                Ok(body)
            }
            Err(ureq::Error::Status(code, resp)) => {
                let mut body = String::new();
                let _ = resp.into_reader().read_to_string(&mut body);
                Err(status_error(url, code, &body))
            }
            Err(ureq::Error::Transport(t)) => Err(transport_error(url, &t)),
        }
    }

    fn rpc(&self, method: &str, params: Value) -> Result<Value, AppError> {
        self.rpc_with_timeout(method, params, None)
    }

    fn rpc_with_timeout(
        &self,
        method: &str,
        params: Value,
        timeout: Option<Duration>,
    ) -> Result<Value, AppError> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let request = RpcRequest {
            jsonrpc: "2.0",
            id,
            method: method.to_string(),
            params,
        };
        let body = serde_json::to_string(&request)
            .map_err(|e| AppError::internal(format!("json-rpc request encode: {e}")))?;

        let http = self
            .agent
            .post(&self.endpoint)
            .set("Content-Type", "application/json")
            .set(A2A_VERSION_HEADER, PROTOCOL_VERSION);
        let mut http = self.apply_token(http);
        if let Some(timeout) = timeout {
            http = http.timeout(timeout.max(Duration::from_millis(1)));
        }
        let response_body = match http.send_string(&body) {
            Ok(resp) => {
                let mut out = String::new();
                resp.into_reader()
                    .read_to_string(&mut out)
                    .map_err(|e| transport_error(&self.endpoint, &e))?;
                out
            }
            Err(ureq::Error::Status(code, resp)) => {
                let mut out = String::new();
                let _ = resp.into_reader().read_to_string(&mut out);
                return Err(status_error(&self.endpoint, code, &out));
            }
            Err(ureq::Error::Transport(t)) => return Err(transport_error(&self.endpoint, &t)),
        };

        let envelope: Value = serde_json::from_str(&response_body).map_err(|e| {
            AppError::integrity(
                "a2a_wire_invalid",
                format!("JSON-RPC response is not JSON: {e}"),
            )
        })?;
        if envelope.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
            return Err(AppError::integrity(
                "a2a_wire_invalid",
                "JSON-RPC response is missing jsonrpc \"2.0\"",
            ));
        }
        if envelope.get("id").and_then(Value::as_u64) != Some(id) {
            return Err(AppError::integrity(
                "a2a_wire_invalid",
                format!("JSON-RPC response id mismatch (sent {id})"),
            ));
        }
        if let Some(err) = envelope.get("error") {
            let code = err.get("code").and_then(Value::as_i64).unwrap_or(-1);
            let message = err
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("unknown agent error");
            return Err(AppError::product(
                "a2a_agent_error",
                format!("agent rejected {method}: {code} {message}"),
            ));
        }
        match envelope.get("result") {
            Some(r) => Ok(r.clone()),
            None => Err(AppError::integrity(
                "a2a_wire_invalid",
                "JSON-RPC response carries neither result nor error",
            )),
        }
    }

    fn apply_token(&self, request: ureq::Request) -> ureq::Request {
        match &self.token {
            Some(t) => request.set("Authorization", &format!("Bearer {t}")),
            None => request,
        }
    }
}

fn transport_error(url: &str, e: &dyn std::fmt::Display) -> AppError {
    AppError::product(
        "a2a_transport_failed",
        format!("request to {url} failed: {e}"),
    )
}

fn status_error(url: &str, code: u16, body: &str) -> AppError {
    // A JSON-RPC error body keeps the agent's code/message; anything else
    // is reported as a plain HTTP failure.
    if let Ok(v) = serde_json::from_str::<Value>(body) {
        if let (Some(e), Some(c), Some(m)) = (
            v.get("error"),
            v.get("error")
                .and_then(|e| e.get("code"))
                .and_then(Value::as_i64),
            v.get("error")
                .and_then(|e| e.get("message"))
                .and_then(Value::as_str),
        ) {
            let _ = e;
            return AppError::product(
                "a2a_agent_error",
                format!("agent returned HTTP {code}: {c} {m}"),
            );
        }
    }
    let snippet: String = body.chars().take(160).collect();
    AppError::product(
        "a2a_http_error",
        format!("HTTP {code} from {url}: {snippet}"),
    )
}
