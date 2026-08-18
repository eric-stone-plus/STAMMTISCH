//! In-process A2A v1.0 agent for offline adapter tests: a plain
//! `std::net::TcpListener` HTTP/1.1 server with a scripted task state
//! machine. Serves the Agent Card at `/.well-known/agent-card.json` and
//! JSON-RPC `SendMessage`/`GetTask` at `/`.
//!
//! The script lets each test steer the wire: task states served per poll,
//! artifacts attached to the terminal snapshot, a missing or wrong-version
//! card, a direct-message reply, a JSON-RPC error, or a garbage body.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use serde_json::{json, Value};

/// Scripted behavior of the fake agent.
#[derive(Debug, Clone, Default)]
pub struct Script {
    /// States returned by successive GetTask calls (the last entry
    /// repeats). Must contain at least one entry.
    pub poll_states: Vec<String>,
    /// Artifacts attached to the terminal (COMPLETED) GetTask snapshot.
    pub artifacts: Vec<Value>,
    /// Agent Card override (None = default card).
    pub card: Option<Value>,
    /// Serve 404 at the card path instead of the card.
    pub card_missing: bool,
    /// JSON-RPC error for SendMessage (None = accept and create a task).
    pub send_error: Option<(i64, String)>,
    /// Reply to SendMessage with a direct Message instead of a Task.
    pub direct_message: bool,
    /// Reply to every POST with a non-JSON body.
    pub garbage_posts: bool,
    /// Override the task id returned by GetTask to exercise binding checks.
    pub get_task_id: Option<String>,
    /// Override every returned task context id.
    pub context_id: Option<String>,
}

#[derive(Default)]
struct Shared {
    script: Script,
    polls_served: u32,
    task_counter: u64,
    /// Bodies of the JSON-RPC requests observed (method-name indexed log).
    pub requests: Vec<(String, Value)>,
    /// The message the agent was sent (SendMessage params.message).
    pub last_message: Option<Value>,
}

pub struct FakeA2a {
    pub endpoint: String,
    pub card_url: String,
    stop: Arc<AtomicBool>,
    shared: Arc<Mutex<Shared>>,
    thread: Option<JoinHandle<()>>,
}

impl FakeA2a {
    /// Bind 127.0.0.1:0 and serve `script` until dropped.
    pub fn start(script: Script) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind test listener");
        listener
            .set_nonblocking(true)
            .expect("nonblocking listener");
        let port = listener.local_addr().expect("local addr").port();
        let endpoint = format!("http://127.0.0.1:{port}/");
        let card_url = format!("http://127.0.0.1:{port}/.well-known/agent-card.json");

        let stop = Arc::new(AtomicBool::new(false));
        let shared = Arc::new(Mutex::new(Shared {
            script,
            ..Default::default()
        }));
        let thread_shared = shared.clone();
        let thread_stop = stop.clone();
        let thread = std::thread::spawn(move || {
            serve(listener, thread_shared, thread_stop);
        });

        Self {
            endpoint,
            card_url,
            stop,
            shared,
            thread: Some(thread),
        }
    }

    /// All JSON-RPC request bodies observed so far, in order.
    pub fn requests(&self) -> Vec<(String, Value)> {
        self.shared.lock().unwrap().requests.clone()
    }

    /// The message carried by the (single) SendMessage call.
    pub fn last_message(&self) -> Value {
        self.shared
            .lock()
            .unwrap()
            .last_message
            .clone()
            .expect("agent never received a message")
    }
}

impl Drop for FakeA2a {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

fn serve(listener: TcpListener, shared: Arc<Mutex<Shared>>, stop: Arc<AtomicBool>) {
    while !stop.load(Ordering::SeqCst) {
        match listener.accept() {
            Ok((stream, _)) => {
                let _ = handle(stream, &shared);
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(5));
            }
            Err(_) => break,
        }
    }
}

fn handle(stream: TcpStream, shared: &Arc<Mutex<Shared>>) -> std::io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    let mut reader = BufReader::new(stream.try_clone()?);

    let mut line = String::new();
    reader.read_line(&mut line)?;
    let mut parts = line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let path = parts.next().unwrap_or("").to_string();

    let mut content_length = 0usize;
    loop {
        let mut header = String::new();
        reader.read_line(&mut header)?;
        let trimmed = header.trim_end();
        if trimmed.is_empty() {
            break;
        }
        if let Some((name, value)) = trimmed.split_once(':') {
            if name.eq_ignore_ascii_case("content-length") {
                content_length = value.trim().parse().unwrap_or(0);
            }
        }
    }
    let mut body = vec![0u8; content_length];
    reader.read_exact(&mut body)?;
    let body_text = String::from_utf8_lossy(&body).to_string();

    let (status, response_body) = route(method.as_str(), path.as_str(), &body_text, shared);
    let response = format!(
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{response_body}",
        response_body.len()
    );
    let mut stream = stream;
    stream.write_all(response.as_bytes())?;
    stream.flush()
}

fn route(method: &str, path: &str, body: &str, shared: &Arc<Mutex<Shared>>) -> (u16, String) {
    if method == "GET" && path == "/.well-known/agent-card.json" {
        let s = shared.lock().unwrap();
        if s.script.card_missing {
            return (404, r#"{"error":"card missing"}"#.to_string());
        }
        return (
            200,
            serde_json::to_string(&default_card(&s.script)).unwrap(),
        );
    }
    if method != "POST" {
        return (404, r#"{"error":"not found"}"#.to_string());
    }

    let mut s = shared.lock().unwrap();
    if s.script.garbage_posts {
        return (200, "not json at all".to_string());
    }
    let request: Value =
        match serde_json::from_str(body) {
            Ok(v) => v,
            Err(_) => return (
                400,
                r#"{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"parse error"}}"#
                    .to_string(),
            ),
        };
    let id = request.get("id").cloned().unwrap_or(Value::Null);
    let rpc_method = request
        .get("method")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let params = request.get("params").cloned().unwrap_or(Value::Null);
    s.requests.push((rpc_method.clone(), params.clone()));

    match rpc_method.as_str() {
        "SendMessage" => {
            if let Some((code, message)) = &s.script.send_error {
                let resp = json!({
                    "jsonrpc": "2.0", "id": id,
                    "error": {"code": code, "message": message}
                });
                return (200, serde_json::to_string(&resp).unwrap());
            }
            if let Some(msg) = params.get("message") {
                s.last_message = Some(msg.clone());
            }
            if s.script.direct_message {
                let resp = json!({
                    "jsonrpc": "2.0", "id": id,
                    "result": {
                        "message": {
                            "messageId": "m-1", "role": "ROLE_AGENT",
                            "parts": [{"text": "direct reply, no task"}]
                        }
                    }
                });
                return (200, serde_json::to_string(&resp).unwrap());
            }
            s.task_counter += 1;
            let task = json!({
                "id": format!("task-{}", s.task_counter),
                "contextId": s.script.context_id.as_ref().map_or_else(
                    || params.get("message").and_then(|m| m.get("contextId")).cloned().unwrap_or(Value::Null),
                    |value| json!(value),
                ),
                "status": {"state": "TASK_STATE_SUBMITTED"},
                "artifacts": [],
                "history": []
            });
            let resp = json!({"jsonrpc": "2.0", "id": id, "result": {"task": task}});
            (200, serde_json::to_string(&resp).unwrap())
        }
        "GetTask" => {
            let task_id = s.script.get_task_id.clone().unwrap_or_else(|| {
                params
                    .get("id")
                    .and_then(Value::as_str)
                    .unwrap_or("task-0")
                    .to_string()
            });
            let states = &s.script.poll_states;
            let state = if states.is_empty() {
                "TASK_STATE_COMPLETED".to_string()
            } else {
                let idx = (s.polls_served as usize).min(states.len() - 1);
                states[idx].clone()
            };
            s.polls_served += 1;
            let artifacts = if state == "TASK_STATE_COMPLETED" {
                s.script.artifacts.clone()
            } else {
                vec![]
            };
            let context_id = s.script.context_id.as_ref().map_or_else(
                || {
                    s.last_message
                        .as_ref()
                        .and_then(|message| message.get("contextId"))
                        .cloned()
                        .unwrap_or(Value::Null)
                },
                |value| json!(value),
            );
            let task = json!({
                "id": task_id,
                "contextId": context_id,
                "status": {"state": state},
                "artifacts": artifacts,
                "history": []
            });
            let resp = json!({"jsonrpc": "2.0", "id": id, "result": task});
            (200, serde_json::to_string(&resp).unwrap())
        }
        other => {
            let resp = json!({
                "jsonrpc": "2.0", "id": id,
                "error": {"code": -32601, "message": format!("method not found: {other}")}
            });
            (200, serde_json::to_string(&resp).unwrap())
        }
    }
}

fn default_card(script: &Script) -> Value {
    if let Some(card) = &script.card {
        return card.clone();
    }
    // The endpoint is filled in by the test once the port is known; the
    // adapter never compares card URLs against the configured endpoint, so
    // a placeholder is honest for offline tests.
    json!({
        "name": "fake-a2a-agent",
        "description": "In-process A2A v1.0 test agent",
        "url": "http://127.0.0.1:1/",
        "version": "1.0.0",
        "supportedInterfaces": [{
            "url": "http://127.0.0.1:1/",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0"
        }],
        "capabilities": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": []
    })
}
