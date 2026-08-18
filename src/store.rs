//! Evidence store mechanics (architecture doc §6): state-root layout,
//! atomic writes, the append-only fsynced event log, and the one-active
//! launch lock.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::error::AppError;
use crate::time;

pub const EVENT_SCHEMA: &str = "stammtisch.run-event.v0";

#[derive(Debug, Clone)]
pub struct StateRoot {
    pub path: PathBuf,
}

impl StateRoot {
    /// `STAMMTISCH_HOME` wins; default `~/.local/share/stammtisch`.
    pub fn resolve() -> Result<Self, AppError> {
        if let Ok(p) = std::env::var("STAMMTISCH_HOME") {
            if p.is_empty() {
                return Err(AppError::usage(
                    "state_root_invalid",
                    "STAMMTISCH_HOME is empty",
                ));
            }
            return Ok(Self {
                path: PathBuf::from(p),
            });
        }
        let home = std::env::var("HOME")
            .map_err(|_| AppError::usage("state_root_invalid", "HOME is not set"))?;
        Ok(Self {
            path: PathBuf::from(home).join(".local/share/stammtisch"),
        })
    }

    pub fn init(&self) -> Result<(), AppError> {
        for d in ["pipelines", "runs", "host", "doctrine"] {
            fs::create_dir_all(self.path.join(d))?;
        }
        Ok(())
    }

    pub fn is_initialized(&self) -> bool {
        self.path.join("runs").is_dir() && self.path.join("host").is_dir()
    }

    pub fn run_dir(&self, run_id: &str) -> PathBuf {
        self.path.join("runs").join(run_id)
    }

    pub fn list_run_ids(&self) -> Result<Vec<String>, AppError> {
        let runs = self.path.join("runs");
        if !runs.is_dir() {
            return Ok(Vec::new());
        }
        let mut ids = Vec::new();
        for entry in fs::read_dir(&runs)? {
            let p = entry?.path();
            if p.is_dir() {
                if let Some(name) = p.file_name().and_then(|n| n.to_str()) {
                    ids.push(name.to_string());
                }
            }
        }
        ids.sort();
        Ok(ids)
    }
}

/// Atomic write: tmp sibling in the same directory, fsync, then rename.
/// Readers never observe a partial file.
pub fn atomic_write(path: &Path, bytes: &[u8]) -> Result<(), AppError> {
    let parent = path
        .parent()
        .ok_or_else(|| AppError::internal(format!("no parent dir for {}", path.display())))?;
    fs::create_dir_all(parent)?;
    let file_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| AppError::internal("non-utf8 file name"))?;
    let tmp = parent.join(format!(".{file_name}.tmp-{}", std::process::id()));
    {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(bytes)?;
        f.sync_all()?;
    }
    fs::rename(&tmp, path)?;
    Ok(())
}

/// Append one line to a file and fsync before returning — the events.jsonl
/// durability contract: the transition is durable before any in-memory
/// state is trusted.
pub fn append_line_fsync(path: &Path, line: &str) -> Result<(), AppError> {
    let mut f = OpenOptions::new().create(true).append(true).open(path)?;
    f.write_all(line.as_bytes())?;
    f.write_all(b"\n")?;
    f.sync_data()?;
    Ok(())
}

/// Writer for a run's event log. Sequence numbers are strict 1..=n.
pub struct EventWriter {
    path: PathBuf,
    run_id: String,
    seq: u64,
}

impl EventWriter {
    pub fn new(run_dir: &Path, run_id: &str) -> Self {
        Self {
            path: run_dir.join("events.jsonl"),
            run_id: run_id.to_string(),
            seq: 0,
        }
    }

    /// Resume an existing log: the next emitted event gets `last_seq + 1`.
    pub fn resume(run_dir: &Path, run_id: &str, last_seq: u64) -> Self {
        Self {
            path: run_dir.join("events.jsonl"),
            run_id: run_id.to_string(),
            seq: last_seq,
        }
    }

    pub fn emit(
        &mut self,
        event_type: &str,
        stage: Option<&str>,
        payload: Value,
    ) -> Result<Value, AppError> {
        self.seq += 1;
        let mut event = serde_json::json!({
            "schema": EVENT_SCHEMA,
            "run_id": self.run_id,
            "seq": self.seq,
            "type": event_type,
            "at": time::now_rfc3339(),
            "payload": payload,
        });
        if let Some(s) = stage {
            event["stage"] = Value::String(s.to_string());
        }
        let line = crate::canon::canonical(&event);
        append_line_fsync(&self.path, &line)?;
        Ok(event)
    }
}

/// Read and strictly validate a run's event log. Any unparseable line,
/// schema violation, or sequence gap means the run dir is corrupt —
/// fail closed, never skip-and-continue.
pub fn read_events(run_dir: &Path) -> Result<Vec<Value>, AppError> {
    let path = run_dir.join("events.jsonl");
    let text = fs::read_to_string(&path).map_err(|e| {
        AppError::integrity(
            "run_corrupt",
            format!("cannot read {}: {e}", path.display()),
        )
    })?;
    let schema: Value =
        serde_json::from_str(crate::schemas::RUN_EVENT).expect("embedded schema parses");
    let mut events = Vec::new();
    let expected_run_id = run_dir
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| AppError::integrity("run_corrupt", "run directory name is not UTF-8"))?;
    for (i, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let event: Value = serde_json::from_str(line).map_err(|e| {
            AppError::integrity(
                "run_corrupt",
                format!("{}: line {} unparseable: {e}", path.display(), i + 1),
            )
        })?;
        let errs = crate::jsonval::violations(&schema, &event);
        if !errs.is_empty() {
            return Err(AppError::integrity(
                "run_corrupt",
                format!(
                    "{}: line {} violates run-event schema: {}",
                    path.display(),
                    i + 1,
                    errs.join("; ")
                ),
            ));
        }
        let seq = event["seq"].as_u64().expect("schema-checked");
        if seq != (events.len() as u64) + 1 {
            return Err(AppError::integrity(
                "run_corrupt",
                format!("{}: sequence gap at line {}", path.display(), i + 1),
            ));
        }
        let observed_run_id = event["run_id"].as_str().expect("schema-checked");
        if observed_run_id != expected_run_id {
            return Err(AppError::integrity(
                "run_corrupt",
                format!(
                    "{}: line {} belongs to run '{}', expected '{}'",
                    path.display(),
                    i + 1,
                    observed_run_id,
                    expected_run_id
                ),
            ));
        }
        events.push(event);
    }
    if events.is_empty() {
        return Err(AppError::integrity(
            "run_corrupt",
            format!("{}: empty event log", path.display()),
        ));
    }
    Ok(events)
}

/// One active run per state root (architecture doc §4). The lock is an
/// exclusive-create file carrying the holder's run id; refusal is clean and
/// immediate — the CLI never waits.
#[derive(Debug)]
pub struct LaunchLock {
    path: PathBuf,
}

impl LaunchLock {
    pub fn acquire(root: &StateRoot, run_id: &str) -> Result<Self, AppError> {
        let path = root.path.join("host").join("launch.lock");
        fs::create_dir_all(path.parent().expect("host dir has parent"))?;
        let holder = serde_json::json!({
            "run_id": run_id,
            "pid": std::process::id(),
            "acquired_at": time::now_rfc3339(),
        });
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut f) => {
                f.write_all(crate::canon::canonical(&holder).as_bytes())?;
                f.sync_all()?;
                Ok(Self { path })
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                let held = fs::read_to_string(&path).unwrap_or_else(|_| "<unreadable>".into());
                Err(AppError::product(
                    "launch_lock_held",
                    format!(
                        "another run is active on this state root ({}); \
                         run `stammtisch reconcile` to clear a stale lock",
                        held.trim()
                    ),
                ))
            }
            Err(e) => Err(AppError::from(e)),
        }
    }

    pub fn lock_path(root: &StateRoot) -> PathBuf {
        root.path.join("host").join("launch.lock")
    }
}

impl Drop for LaunchLock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_root() -> StateRoot {
        let p = std::env::temp_dir().join(format!(
            "stammtisch-store-{}",
            crate::ids::uuid_v7().unwrap()
        ));
        let root = StateRoot { path: p };
        root.init().unwrap();
        root
    }

    #[test]
    fn atomic_write_roundtrip_and_replace() {
        let root = tmp_root();
        let f = root.path.join("runs").join("x.json");
        atomic_write(&f, b"one").unwrap();
        assert_eq!(fs::read(&f).unwrap(), b"one");
        atomic_write(&f, b"two").unwrap();
        assert_eq!(fs::read(&f).unwrap(), b"two");
        // no tmp siblings left behind
        let leftovers: Vec<_> = fs::read_dir(f.parent().unwrap())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().contains(".tmp-"))
            .collect();
        assert!(leftovers.is_empty());
        fs::remove_dir_all(&root.path).ok();
    }

    #[test]
    fn launch_lock_is_exclusive() {
        let root = tmp_root();
        let a = LaunchLock::acquire(&root, "run-a").unwrap();
        let err = LaunchLock::acquire(&root, "run-b").unwrap_err();
        assert_eq!(err.code, "launch_lock_held");
        drop(a); // releases
        let _b = LaunchLock::acquire(&root, "run-b").unwrap();
        fs::remove_dir_all(&root.path).ok();
    }

    #[test]
    fn event_log_strict_read() {
        let root = tmp_root();
        let run_dir = root.path.join("runs").join("r1");
        fs::create_dir_all(&run_dir).unwrap();
        let mut w = EventWriter::new(&run_dir, "r1");
        w.emit("run.created", None, serde_json::json!({})).unwrap();
        w.emit("run.staged", None, serde_json::json!({})).unwrap();
        let events = read_events(&run_dir).unwrap();
        assert_eq!(events.len(), 2);
        assert_eq!(events[1]["seq"], 2);

        // Corruption: garbage line => fail closed.
        append_line_fsync(&run_dir.join("events.jsonl"), "garbage").unwrap();
        let e = read_events(&run_dir).unwrap_err();
        assert_eq!(e.code, "run_corrupt");
        fs::remove_dir_all(&root.path).ok();
    }

    #[test]
    fn sequence_gap_detected() {
        let root = tmp_root();
        let run_dir = root.path.join("runs").join("r2");
        fs::create_dir_all(&run_dir).unwrap();
        let line = crate::canon::canonical(&serde_json::json!({
            "schema": EVENT_SCHEMA, "run_id": "r2", "seq": 5,
            "type": "run.created", "at": "2026-08-09T00:00:00.000Z", "payload": {}
        }));
        append_line_fsync(&run_dir.join("events.jsonl"), &line).unwrap();
        let e = read_events(&run_dir).unwrap_err();
        assert_eq!(e.code, "run_corrupt");
        fs::remove_dir_all(&root.path).ok();
    }

    #[test]
    fn mixed_run_ids_are_rejected() {
        let root = tmp_root();
        let run_dir = root.path.join("runs").join("r3");
        fs::create_dir_all(&run_dir).unwrap();
        let mut writer = EventWriter::new(&run_dir, "r3");
        writer
            .emit("run.created", None, serde_json::json!({}))
            .unwrap();
        let mixed = crate::canon::canonical(&serde_json::json!({
            "schema": EVENT_SCHEMA,
            "run_id": "different-run",
            "seq": 2,
            "type": "run.staged",
            "at": "2026-08-09T00:00:00.000Z",
            "payload": {}
        }));
        append_line_fsync(&run_dir.join("events.jsonl"), &mixed).unwrap();
        let error = read_events(&run_dir).unwrap_err();
        assert_eq!(error.code, "run_corrupt");
        assert!(error.message.contains("different-run"));
        fs::remove_dir_all(&root.path).ok();
    }
}
