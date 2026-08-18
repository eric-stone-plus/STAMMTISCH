//! Typed A2A v1.0 wire model (JSON-RPC binding), shaped after the normative
//! `specification/a2a.proto` of the a2aproject/A2A specification
//! (a2a-protocol.org). Only the objects the adapter observes or sends are
//! modeled; unknown fields are ignored by design (the protocol reserves
//! extension room) and the verbatim upstream bytes are pinned by digest in
//! the versioned A2A invocation receipt instead.

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// A2A task lifecycle (TaskState in the normative proto).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
pub enum TaskState {
    #[serde(rename = "TASK_STATE_UNSPECIFIED")]
    Unspecified,
    #[serde(rename = "TASK_STATE_SUBMITTED")]
    Submitted,
    #[serde(rename = "TASK_STATE_WORKING")]
    Working,
    #[serde(rename = "TASK_STATE_INPUT_REQUIRED")]
    InputRequired,
    #[serde(rename = "TASK_STATE_AUTH_REQUIRED")]
    AuthRequired,
    #[serde(rename = "TASK_STATE_COMPLETED")]
    Completed,
    #[serde(rename = "TASK_STATE_FAILED")]
    Failed,
    #[serde(rename = "TASK_STATE_CANCELED")]
    Canceled,
    #[serde(rename = "TASK_STATE_REJECTED")]
    Rejected,
}

impl TaskState {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Unspecified => "TASK_STATE_UNSPECIFIED",
            Self::Submitted => "TASK_STATE_SUBMITTED",
            Self::Working => "TASK_STATE_WORKING",
            Self::InputRequired => "TASK_STATE_INPUT_REQUIRED",
            Self::AuthRequired => "TASK_STATE_AUTH_REQUIRED",
            Self::Completed => "TASK_STATE_COMPLETED",
            Self::Failed => "TASK_STATE_FAILED",
            Self::Canceled => "TASK_STATE_CANCELED",
            Self::Rejected => "TASK_STATE_REJECTED",
        }
    }
}

/// Agent Card (AgentCard): discovery document served at the well-known URL.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentCard {
    pub name: String,
    #[serde(default)]
    pub description: String,
    #[serde(default)]
    pub supported_interfaces: Vec<AgentInterface>,
    #[serde(default)]
    pub version: String,
    #[serde(default)]
    pub capabilities: AgentCapabilities,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentInterface {
    pub url: String,
    #[serde(default)]
    pub protocol_binding: String,
    #[serde(default)]
    pub tenant: Option<String>,
    #[serde(default)]
    pub protocol_version: String,
}

#[derive(Debug, Clone, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct AgentCapabilities {
    #[serde(default)]
    pub streaming: bool,
    #[serde(default)]
    pub push_notifications: bool,
    #[serde(default)]
    pub extended_agent_card: bool,
}

/// The task object (Task): unit of work with lifecycle state and artifacts.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Task {
    pub id: String,
    #[serde(default)]
    pub context_id: Option<String>,
    pub status: TaskStatus,
    #[serde(default)]
    pub artifacts: Vec<Artifact>,
    #[serde(default)]
    pub history: Vec<Message>,
    #[serde(default)]
    pub metadata: Option<Value>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TaskStatus {
    pub state: TaskState,
    #[serde(default)]
    pub message: Option<Message>,
    #[serde(default)]
    pub timestamp: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Message {
    pub message_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub context_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub task_id: Option<String>,
    pub role: String,
    pub parts: Vec<Part>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metadata: Option<Value>,
}

/// One part of a message or artifact (Part with its `content` oneof).
/// Exactly one of text/raw/url/data carries content; media_type names the
/// MIME type and filename the artifact's suggested name.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct Part {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub raw: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub metadata: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub filename: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub media_type: Option<String>,
}

/// Task output (Artifact): named collection of parts.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Artifact {
    pub artifact_id: String,
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub description: Option<String>,
    pub parts: Vec<Part>,
    #[serde(default)]
    pub metadata: Option<Value>,
    #[serde(default)]
    pub extensions: Vec<String>,
}

/// SendMessage result: exactly one of task or message per the oneof
/// `SendMessageResponse.payload`. Presence is validated by the adapter.
#[derive(Debug, Clone, Deserialize, Default)]
pub struct SendMessageResponse {
    #[serde(default)]
    pub task: Option<Task>,
    #[serde(default)]
    pub message: Option<Message>,
}

/// JSON-RPC 2.0 request envelope, built (never parsed) by the client.
#[derive(Debug, Clone, Serialize)]
pub struct RpcRequest {
    pub jsonrpc: &'static str,
    pub id: u64,
    pub method: String,
    pub params: Value,
}

/// JSON-RPC 2.0 error object.
#[derive(Debug, Clone, Deserialize)]
pub struct RpcError {
    pub code: i64,
    pub message: String,
    #[serde(default)]
    pub data: Option<Value>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn task_state_round_trips() {
        let t: TaskStatus = serde_json::from_str(r#"{"state":"TASK_STATE_WORKING"}"#).unwrap();
        assert_eq!(t.state, TaskState::Working);
        assert_eq!(t.state.as_str(), "TASK_STATE_WORKING");

        let s: SendMessageResponse = serde_json::from_str(
            r#"{"task":{"id":"t1","status":{"state":"TASK_STATE_SUBMITTED"}}}"#,
        )
        .unwrap();
        assert_eq!(s.task.unwrap().id, "t1");
    }

    #[test]
    fn unknown_state_is_a_parse_error() {
        // Fail closed: an unknown state string must never be mapped by
        // guessing — the adapter turns this parse error into a halt.
        let e = serde_json::from_str::<TaskStatus>(r#"{"state":"TASK_STATE_TELEPORTED"}"#);
        assert!(e.is_err());
    }

    #[test]
    fn part_variants_discriminate_by_member_presence() {
        let p: Part =
            serde_json::from_str(r#"{"data":{"x":1},"mediaType":"application/json"}"#).unwrap();
        assert_eq!(p.data, Some(serde_json::json!({"x": 1})));
        assert_eq!(p.media_type.as_deref(), Some("application/json"));
    }
}
