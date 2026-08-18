//! `--json` envelope printing. Machine output always goes to stdout as one
//! envelope object; human diagnostics stay on stderr regardless of mode.

use serde_json::{json, Value};

use crate::error::AppError;

pub fn ok(command: &str, data: Value) -> Value {
    json!({"ok": true, "command": command, "data": data})
}

pub fn err(command: &str, e: &AppError) -> Value {
    json!({
        "ok": false,
        "command": command,
        "error": {"code": e.code, "message": e.message}
    })
}

pub fn print(envelope: &Value) {
    println!(
        "{}",
        serde_json::to_string_pretty(envelope).expect("envelope serializes")
    );
}

/// Human-side note; always stderr, never mixed into machine output.
pub fn note(msg: impl AsRef<str>) {
    eprintln!("stammtisch: {}", msg.as_ref());
}
