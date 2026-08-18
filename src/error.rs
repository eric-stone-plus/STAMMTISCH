//! Error taxonomy and CLI exit-code mapping (architecture doc §8):
//!
//! - `0` completed / clean observation
//! - `1` product-failure family (adapter failure, launch-lock contention)
//! - `2` blocked / halted / integrity failure (digest drift, corrupt state,
//!   unparseable receipt, unknown contract revision, verification failure)
//! - `3` usage / contract error (bad arguments, invalid pipeline, unknown
//!   run id, doctrine binding failure)

use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Usage,
    ProductFailure,
    Integrity,
    Internal,
}

#[derive(Debug)]
pub struct AppError {
    pub kind: Kind,
    pub code: &'static str,
    pub message: String,
}

impl AppError {
    pub fn usage(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            kind: Kind::Usage,
            code,
            message: message.into(),
        }
    }
    pub fn product(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            kind: Kind::ProductFailure,
            code,
            message: message.into(),
        }
    }
    pub fn integrity(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            kind: Kind::Integrity,
            code,
            message: message.into(),
        }
    }
    pub fn internal(message: impl Into<String>) -> Self {
        Self {
            kind: Kind::Internal,
            code: "internal_error",
            message: message.into(),
        }
    }

    /// CLI exit code per architecture doc §8.
    pub fn exit_code(&self) -> i32 {
        match self.kind {
            Kind::Usage => 3,
            Kind::ProductFailure => 1,
            Kind::Integrity => 2,
            // Internal errors are integrity-shaped: never pretend success.
            Kind::Internal => 2,
        }
    }
}

impl fmt::Display for AppError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for AppError {}

impl From<std::io::Error> for AppError {
    fn from(e: std::io::Error) -> Self {
        AppError::internal(format!("io: {e}"))
    }
}

impl From<serde_json::Error> for AppError {
    fn from(e: serde_json::Error) -> Self {
        AppError::internal(format!("json: {e}"))
    }
}
