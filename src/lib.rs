//! STAMMTISCH P0 skeleton — deterministic pipeline state machine, evidence
//! store, gate evaluator, and offline-verifiable deliverable bundles.
//!
//! Dependency discipline: serde/serde_json/sha2 only. No async runtime, no
//! network, no argument-parser crate. `serde_json` is used WITHOUT the
//! `preserve_order` feature, so every `Value` object is a BTreeMap and
//! serializes with sorted keys — that property is the canonical form used
//! for all digests (see [`canon`]).

pub mod adapters;
pub mod bundle;
pub mod canon;
pub mod cmd;
pub mod contracts;
pub mod cost;
pub mod doctrine;
pub mod envelope;
pub mod error;
pub mod gates;
pub mod ids;
pub mod jsonval;
pub mod pattern;
pub mod pipeline;
pub mod random;
pub mod runner;
pub mod schemas;
pub mod store;
pub mod time;
