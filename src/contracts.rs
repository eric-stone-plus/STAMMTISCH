//! Product receipt contracts, pinned by revision (architecture doc §5, §12).
//!
//! Every receipt carries a `"schema"` field naming its contract revision.
//! Acceptance is exact: a known revision is structurally validated against
//! the pinned schema below; an unknown revision is a hard error — the
//! runner halts, it never parses best-effort (conformance item 10).

use serde_json::Value;

/// Contract revision the HIGHBALL adapter emits (Action Packet
/// authorization decision).
pub const HIGHBALL_ACTION_PACKET_V1: &str = r#"{
  "type": "object",
  "required": ["schema", "packet_id", "route", "decision"],
  "additionalProperties": false,
  "properties": {
    "schema": {"const": "highball.action-packet.v1"},
    "packet_id": {"type": "string", "minLength": 1},
    "route": {"enum": ["direct-evidence", "human-review", "block"]},
    "decision": {"enum": ["AUTHORIZED", "DENIED"]},
    "action_decision": {"enum": ["pass", "review", "block"]},
    "packet_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    "reasons": {"type": "array", "items": {"type": "string"}}
  }
}"#;

/// Contract revision the doctrine adapter emits when materializing the
/// stage-0 brief.
pub const DOCTRINE_BRIEF_V0: &str = r#"{
  "type": "object",
  "required": ["schema", "pack", "pack_sha256", "brief_sha256"],
  "additionalProperties": false,
  "properties": {
    "schema": {"const": "doctrine.brief.v0"},
    "pack": {"type": "string", "minLength": 1},
    "pack_version": {"type": "string"},
    "pack_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    "brief_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
  }
}"#;

/// Contract revision the GALAHAD paper adapter emits.
pub const GALAHAD_PAPER_SESSION_V1: &str = r#"{
  "type": "object",
  "required": ["schema", "run_id", "symbol", "verdict", "mode"],
  "additionalProperties": false,
  "properties": {
    "schema": {"const": "galahad.paper-session.v1"},
    "run_id": {"type": "string", "minLength": 1},
    "symbol": {"type": "string", "minLength": 1},
    "verdict": {"enum": ["GO", "NO-GO"]},
    "mode": {"const": "paper"},
    "summary_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
  }
}"#;

/// Contract revision the GALAHAD adapter emits for an accepted Binance
/// TESTNET session (`--engine nautilus_live`, summary `mode: "testnet"`).
/// Mainnet is never receipted: any other mode is refused upstream of the
/// receipt (see src/adapters/galahad.rs).
pub const GALAHAD_TESTNET_SESSION_V1: &str = r#"{
  "type": "object",
  "required": ["schema", "run_id", "symbol", "verdict", "mode", "venue", "reconciliation", "summary_sha256"],
  "additionalProperties": false,
  "properties": {
    "schema": {"const": "galahad.testnet-session.v1"},
    "run_id": {"type": "string", "minLength": 1},
    "symbol": {"type": "string", "minLength": 1},
    "verdict": {"enum": ["GO", "NO-GO"]},
    "mode": {"const": "testnet"},
    "venue": {"type": "string", "minLength": 1},
    "reconciliation": {
      "type": "object",
      "required": ["orders_submitted", "orders_filled", "position_mismatch"],
      "additionalProperties": false,
      "properties": {
        "orders_submitted": {"type": "integer", "minimum": 0},
        "orders_filled": {"type": "integer", "minimum": 0},
        "position_mismatch": {"type": "boolean"}
      }
    },
    "summary_sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
  }
}"#;

const KNOWN: &[(&str, &str)] = &[
    ("highball.action-packet.v1", HIGHBALL_ACTION_PACKET_V1),
    ("doctrine.brief.v0", DOCTRINE_BRIEF_V0),
    ("galahad.paper-session.v1", GALAHAD_PAPER_SESSION_V1),
    ("galahad.testnet-session.v1", GALAHAD_TESTNET_SESSION_V1),
    // Wire-observation wrapper for A2A product invocations (see
    // docs/protocol-layer.md); the schema text lives in schemas/ as the
    // source of truth, embedded at compile time.
    ("a2a.invocation.v1", crate::schemas::A2A_INVOCATION),
    ("a2a.invocation.v2", crate::schemas::A2A_INVOCATION_V2),
];

#[derive(Debug)]
pub enum ReceiptError {
    Unparseable(String),
    MissingRevision,
    UnknownRevision(String),
    SchemaInvalid(String, Vec<String>),
    BindingInvalid(String, String),
}

impl std::fmt::Display for ReceiptError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unparseable(e) => write!(f, "unparseable receipt: {e}"),
            Self::MissingRevision => write!(f, "receipt carries no \"schema\" revision field"),
            Self::UnknownRevision(r) => write!(f, "unknown contract revision: {r}"),
            Self::SchemaInvalid(r, errs) => {
                write!(f, "receipt invalid against {r}: {}", errs.join("; "))
            }
            Self::BindingInvalid(r, detail) => {
                write!(f, "receipt has invalid {r} binding: {detail}")
            }
        }
    }
}

/// Validate receipt bytes against their pinned contract revision.
/// Returns the revision on success. Fails closed on every ambiguity.
pub fn validate_receipt(bytes: &[u8]) -> Result<(String, Value), ReceiptError> {
    let value: Value =
        serde_json::from_slice(bytes).map_err(|e| ReceiptError::Unparseable(e.to_string()))?;
    let revision = match value.get("schema").and_then(Value::as_str) {
        Some(r) => r.to_string(),
        None => return Err(ReceiptError::MissingRevision),
    };
    let schema_text = KNOWN
        .iter()
        .find(|(rev, _)| *rev == revision)
        .map(|(_, s)| *s)
        .ok_or_else(|| ReceiptError::UnknownRevision(revision.clone()))?;
    let schema: Value = serde_json::from_str(schema_text).expect("embedded schema parses");
    let errs = crate::jsonval::violations(&schema, &value);
    if !errs.is_empty() {
        return Err(ReceiptError::SchemaInvalid(revision, errs));
    }
    if matches!(revision.as_str(), "a2a.invocation.v1" | "a2a.invocation.v2") {
        let actual = crate::canon::sha256_value_prefixed(&value["upstream"]);
        let pinned = value["upstream_sha256"].as_str().unwrap_or("");
        if actual != pinned {
            return Err(ReceiptError::BindingInvalid(
                revision,
                format!("upstream hashes to {actual}, receipt pins {pinned}"),
            ));
        }
    }
    Ok((revision, value))
}

pub fn known_revisions() -> Vec<&'static str> {
    KNOWN.iter().map(|(r, _)| *r).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn highball_packet(packet_id: &str, decision: &str) -> Value {
        json!({
            "schema": "highball.action-packet.v1",
            "packet_id": packet_id,
            "route": "direct-evidence",
            "decision": decision,
            "action_decision": if decision == "AUTHORIZED" { "pass" } else { "block" },
            "packet_sha256": format!("sha256:{}", "3".repeat(64)),
            "reasons": []
        })
    }

    #[test]
    fn known_receipt_accepted() {
        let r = highball_packet("pkt-1", "AUTHORIZED");
        let (rev, _) = validate_receipt(serde_json::to_string(&r).unwrap().as_bytes()).unwrap();
        assert_eq!(rev, "highball.action-packet.v1");
    }

    #[test]
    fn highball_packet_accepted_and_rejected() {
        let r = highball_packet("pkt-2", "DENIED");
        let (rev, _) = validate_receipt(serde_json::to_string(&r).unwrap().as_bytes()).unwrap();
        assert_eq!(rev, "highball.action-packet.v1");

        let mut bad = r.clone();
        bad.as_object_mut().unwrap().remove("packet_id");
        assert!(matches!(
            validate_receipt(serde_json::to_string(&bad).unwrap().as_bytes()),
            Err(ReceiptError::SchemaInvalid(..))
        ));
        let mut bad2 = r.clone();
        bad2["decision"] = json!("MAYBE"); // not a HIGHBALL decision
        assert!(matches!(
            validate_receipt(serde_json::to_string(&bad2).unwrap().as_bytes()),
            Err(ReceiptError::SchemaInvalid(..))
        ));
    }

    #[test]
    fn schema_invalid_rejected() {
        let r = json!({"schema": "highball.action-packet.v1", "decision": "MAYBE"});
        let err = validate_receipt(serde_json::to_string(&r).unwrap().as_bytes()).unwrap_err();
        assert!(matches!(err, ReceiptError::SchemaInvalid(..)));
    }

    #[test]
    fn unknown_revision_rejected() {
        let r = json!({"schema": "highball.action-packet.v9", "decision": "AUTHORIZED"});
        let err = validate_receipt(serde_json::to_string(&r).unwrap().as_bytes()).unwrap_err();
        assert!(matches!(err, ReceiptError::UnknownRevision(_)));
    }

    fn galahad_testnet_receipt() -> Value {
        json!({
            "schema": "galahad.testnet-session.v1",
            "run_id": "20260904T000000Z",
            "symbol": "BTCUSDT",
            "verdict": "GO",
            "mode": "testnet",
            "venue": "BINANCE",
            "reconciliation": {
                "orders_submitted": 3,
                "orders_filled": 2,
                "position_mismatch": false
            },
            "summary_sha256": format!("sha256:{}", "4".repeat(64))
        })
    }

    #[test]
    fn galahad_testnet_receipt_accepted() {
        let r = galahad_testnet_receipt();
        let (rev, _) = validate_receipt(serde_json::to_string(&r).unwrap().as_bytes()).unwrap();
        assert_eq!(rev, "galahad.testnet-session.v1");
    }

    #[test]
    fn galahad_testnet_receipt_rejects_drift() {
        // "live" is not a receiptable mode; the registry fails closed.
        let mut live = galahad_testnet_receipt();
        live["mode"] = json!("live");
        assert!(matches!(
            validate_receipt(serde_json::to_string(&live).unwrap().as_bytes()),
            Err(ReceiptError::SchemaInvalid(..))
        ));
        // Missing reconciliation digest fields fail closed.
        let mut bare = galahad_testnet_receipt();
        bare["reconciliation"]
            .as_object_mut()
            .unwrap()
            .remove("position_mismatch");
        assert!(matches!(
            validate_receipt(serde_json::to_string(&bare).unwrap().as_bytes()),
            Err(ReceiptError::SchemaInvalid(..))
        ));
        // A paper receipt must not claim the testnet revision.
        let mut paper = galahad_testnet_receipt();
        paper["mode"] = json!("paper");
        assert!(matches!(
            validate_receipt(serde_json::to_string(&paper).unwrap().as_bytes()),
            Err(ReceiptError::SchemaInvalid(..))
        ));
    }

    #[test]
    fn garbage_bytes_rejected() {
        assert!(matches!(
            validate_receipt(b"not json"),
            Err(ReceiptError::Unparseable(_))
        ));
        assert!(matches!(
            validate_receipt(b"{}"),
            Err(ReceiptError::MissingRevision)
        ));
    }

    fn a2a_receipt(operation: &str, task_id: Option<&str>) -> Value {
        let mut r = json!({
            "schema": "a2a.invocation.v1",
            "host": {
                "endpoint": "http://127.0.0.1:9900/",
                "card_url": "http://127.0.0.1:9900/.well-known/agent-card.json",
                "card_sha256": format!("sha256:{}", "1".repeat(64)),
                "agent": "test-agent",
                "protocol_version": "1.0"
            },
            "stage": "review",
            "operation": operation,
            "observed_at": "2026-08-13T00:00:00.000Z",
            "invocation_id": "019b4e5a-2c3d-7e8f-9a0b-1c2d3e4f5a6b",
            "upstream": {"id": "t1"},
            "upstream_sha256": crate::canon::sha256_value_prefixed(&json!({"id": "t1"}))
        });
        if let Some(tid) = task_id {
            r["task_id"] = json!(tid);
            r["context_id"] = json!("run-1");
            r["task_state"] = json!("TASK_STATE_WORKING");
        }
        r
    }

    #[test]
    fn a2a_invocation_accepted() {
        // Task ops must carry task_id + task_state; card_discovery must not.
        for (op, task) in [
            ("send_message", Some("t-1")),
            ("get_task", Some("t-1")),
            ("card_discovery", None),
        ] {
            let r = a2a_receipt(op, task);
            let (rev, _) = validate_receipt(crate::canon::canonical(&r).as_bytes()).unwrap();
            assert_eq!(rev, "a2a.invocation.v1");
        }
    }

    #[test]
    fn a2a_invocation_rejects_missing_task_fields() {
        let mut r = a2a_receipt("get_task", Some("t-1"));
        r.as_object_mut().unwrap().remove("task_state");
        assert!(matches!(
            validate_receipt(crate::canon::canonical(&r).as_bytes()),
            Err(ReceiptError::SchemaInvalid(..))
        ));

        // A card_discovery receipt carrying task fields is rejected too:
        // the wrapper must not claim a task that the operation never saw.
        let mut c = a2a_receipt("card_discovery", Some("t-1"));
        c.as_object_mut().unwrap().remove("task_state");
        assert!(matches!(
            validate_receipt(crate::canon::canonical(&c).as_bytes()),
            Err(ReceiptError::SchemaInvalid(..))
        ));
    }

    #[test]
    fn a2a_v1_legacy_context_optional_and_upstream_binding_checked() {
        let mut legacy = a2a_receipt("get_task", Some("t-1"));
        legacy.as_object_mut().unwrap().remove("context_id");
        validate_receipt(crate::canon::canonical(&legacy).as_bytes()).unwrap();

        legacy["upstream"]["id"] = json!("tampered");
        assert!(matches!(
            validate_receipt(crate::canon::canonical(&legacy).as_bytes()),
            Err(ReceiptError::BindingInvalid(..))
        ));
    }

    #[test]
    fn a2a_v2_requires_context_for_task_operations() {
        let mut receipt = a2a_receipt("get_task", Some("t-1"));
        receipt["schema"] = json!("a2a.invocation.v2");
        validate_receipt(crate::canon::canonical(&receipt).as_bytes()).unwrap();

        receipt.as_object_mut().unwrap().remove("context_id");
        assert!(matches!(
            validate_receipt(crate::canon::canonical(&receipt).as_bytes()),
            Err(ReceiptError::SchemaInvalid(..))
        ));
    }
}
