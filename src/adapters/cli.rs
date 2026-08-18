//! Locate and invoke a product CLI. Adapters own product contact; this
//! module only shells out and parses JSON stdout. A non-zero exit is
//! accepted when stdout is still a JSON object (HIGHBALL prints a packet
//! whose `action_decision` is not `pass`).

use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::Value;

use crate::error::AppError;

pub fn resolve_bin(env_key: &str, name: &str) -> Result<PathBuf, AppError> {
    if let Ok(raw) = std::env::var(env_key) {
        let path = PathBuf::from(raw);
        if path.is_file() {
            return Ok(path);
        }
        return Err(AppError::product(
            "product_cli_missing",
            format!("{env_key}={path} is not a file", path = path.display()),
        ));
    }
    if let Some(found) = which(name) {
        return Ok(found);
    }
    Err(AppError::product(
        "product_cli_missing",
        format!("'{name}' not on PATH and {env_key} is unset"),
    ))
}

pub fn resolve_script(env_dir_key: &str, rel: &str) -> Result<PathBuf, AppError> {
    if let Ok(raw) = std::env::var(env_dir_key) {
        let path = PathBuf::from(raw).join(rel);
        if path.is_file() {
            return Ok(path);
        }
        return Err(AppError::product(
            "product_cli_missing",
            format!(
                "{env_dir_key} does not contain {rel} (looked at {})",
                path.display()
            ),
        ));
    }
    if let Some(found) = which(rel) {
        return Ok(found);
    }
    // Also accept the basename on PATH (build-action-packet.py).
    if let Some(name) = Path::new(rel).file_name().and_then(|s| s.to_str()) {
        if let Some(found) = which(name) {
            return Ok(found);
        }
    }
    Err(AppError::product(
        "product_cli_missing",
        format!("'{rel}' not found; set {env_dir_key} to the product root"),
    ))
}

pub fn run_json(bin: &Path, args: &[&str]) -> Result<(i32, Value), AppError> {
    run_json_env(bin, args, &[])
}

pub fn run_json_env(
    bin: &Path,
    args: &[&str],
    extra_env: &[(&str, &str)],
) -> Result<(i32, Value), AppError> {
    let mut cmd = Command::new(bin);
    cmd.args(args);
    for (k, v) in extra_env {
        cmd.env(k, v);
    }
    let out = cmd.output().map_err(|e| {
        AppError::product(
            "product_cli_failed",
            format!("spawn {} {}: {e}", bin.display(), args.join(" ")),
        )
    })?;
    let code = out.status.code().unwrap_or(-1);
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    let value: Value = serde_json::from_str(stdout.trim()).map_err(|e| {
        AppError::product(
            "product_cli_invalid",
            format!(
                "{} {} exited {code}; stdout is not JSON ({e}): {stdout}{tail}",
                bin.display(),
                args.join(" "),
                tail = if stderr.is_empty() {
                    String::new()
                } else {
                    format!(" stderr={stderr}")
                }
            ),
        )
    })?;
    Ok((code, value))
}

pub fn resolve_python(env_key: &str) -> Result<PathBuf, AppError> {
    if let Ok(raw) = std::env::var(env_key) {
        let path = PathBuf::from(raw);
        if path.is_file() {
            return Ok(path);
        }
        return Err(AppError::product(
            "product_cli_missing",
            format!("{env_key}={path} is not a file", path = path.display()),
        ));
    }
    which("python3").ok_or_else(|| AppError::product("product_cli_missing", "python3 not on PATH"))
}

pub fn run_python_json(script: &Path, args: &[&str]) -> Result<(i32, Value), AppError> {
    run_python_json_env(script, args, &[])
}

pub fn run_python_json_env(
    script: &Path,
    args: &[&str],
    extra_env: &[(&str, &str)],
) -> Result<(i32, Value), AppError> {
    let python = which("python3")
        .ok_or_else(|| AppError::product("product_cli_missing", "python3 not on PATH"))?;
    let mut all = vec![script.to_str().unwrap_or("")];
    all.extend_from_slice(args);
    run_json_env(&python, &all, extra_env)
}

pub(crate) fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let cand = dir.join(name);
        if cand.is_file() {
            return Some(cand);
        }
    }
    None
}
