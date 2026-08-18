//! Product adapters (architecture doc §5) — the only code allowed to touch
//! a product. The runner drives every adapter through the same
//! preflight/invoke/poll/collect contract; receipts and artifacts are
//! validated by the runner before acceptance.
//!
//! - [`fake`]: offline fakes with contract-accurate canned receipts,
//!   steered by doctrine fixtures (P0).

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::Value;

use crate::doctrine::DoctrinePack;
use crate::error::{AppError, Kind};
use crate::pipeline::Stage;

pub mod a2a;
pub mod cli;
pub mod fake;
pub mod galahad;
pub mod highball;

/// Everything a product invocation may observe: stage config, doctrine
/// binding, digests of the declared input artifacts, and the run directory
/// (real adapters materialize invocation inputs there; artifact bytes live
/// content-addressed under `artifacts/`).
pub struct StageContext<'a> {
    pub run_id: &'a str,
    pub pipeline_id: &'a str,
    pub stage: &'a Stage,
    pub doctrine: &'a DoctrinePack,
    /// artifact name -> "sha256:<hex>"
    pub inputs: &'a BTreeMap<String, String>,
    pub run_dir: &'a Path,
}

/// Product refusal (e.g. HIGHBALL DENIED) is a verdict, not an error.
#[derive(Debug, Clone)]
pub enum Verdict {
    Proceed,
    Refused(String),
}

pub struct Collected {
    /// Contract-ready receipts (validated by the runner before acceptance).
    pub receipts: Vec<Value>,
    /// (artifact name, JSON content)
    pub artifacts: Vec<(String, Value)>,
    pub verdict: Verdict,
}

#[derive(Debug, Clone)]
pub struct InvocationHandle {
    pub id: String,
    pub product: String,
}

pub enum PollState {
    Completed,
    Failed(String),
    /// Fail-closed stop (P3): the adapter observed an ambiguity it must not
    /// resolve by guessing — e.g. a timed-out or contract-violating product
    /// invocation. The runner records a durable HALTED state.
    Halted(String),
}

pub trait Adapter {
    fn preflight(&self, ctx: &StageContext) -> Result<(), AppError>;
    fn invoke(&self, ctx: &StageContext) -> Result<InvocationHandle, AppError>;
    fn poll(&self, handle: &InvocationHandle) -> PollState;
    fn collect(&self, handle: &InvocationHandle, ctx: &StageContext)
        -> Result<Collected, AppError>;

    /// Drain receipts accumulated outside a successful `collect` (a real
    /// adapter observes preflight/start/status receipts even when the stage
    /// later fails or halts). The runner persists drained receipts before
    /// recording the terminal event, so failure paths keep their evidence.
    /// Default: nothing accumulated (fakes emit receipts only via collect).
    fn drain_receipts(&self) -> Vec<Value> {
        Vec::new()
    }
}

/// Fake-only selection by product name (P0 behavior; used by adapter unit
/// tests). The runner selects through [`for_stage`], which honors the
/// stage's runtime configuration.
pub fn for_product(product: &str) -> Result<Box<dyn Adapter>, AppError> {
    match product {
        "doctrine" => Ok(Box::new(fake::DoctrineFake)),
        "highball" => Ok(Box::new(fake::HighballFake)),
        "quinte" => Err(AppError::usage(
            "quinte_runtime_required",
            "product 'quinte' has no offline fake; it runs only against a \
             stage.runtime A2A binding (docs/protocol-layer.md)",
        )),
        other => Err(AppError::usage(
            "product_unknown",
            format!("no adapter for product '{other}'"),
        )),
    }
}

/// Runner-side selection: a `runtime` binding resolves to the wire adapter
/// (A2A v1.0). Otherwise highball/galahad use the shipped product CLI
/// unless the stage sets `adapter: "fake"`. Doctrine stays on its offline
/// fake. No ambient environment selection — the pipeline spec declares the path.
pub fn for_stage(stage: &Stage) -> Result<Box<dyn Adapter>, AppError> {
    if let Some(runtime) = &stage.runtime {
        return match runtime.protocol.as_str() {
            "a2a" => Ok(Box::new(a2a::A2aAdapter::new(runtime)?)),
            other => Err(AppError::usage(
                "runtime_protocol_unknown",
                format!(
                    "stage '{}' declares unknown runtime protocol '{other}'",
                    stage.id
                ),
            )),
        };
    }
    if stage.adapter.as_deref() == Some("fake") {
        return for_product(&stage.product);
    }
    match stage.product.as_str() {
        "highball" => Ok(Box::new(highball::HighballAdapter::new(stage)?)),
        "galahad" => Ok(Box::new(galahad::GalahadAdapter::new(stage)?)),
        _ => for_product(&stage.product),
    }
}

/// Mid-run adapter failure → terminal state mapping (P3): integrity and
/// internal failures (binding mismatch, unparseable receipt, digest drift)
/// halt; ordinary product failures (spawn failure, refused launch, failed
/// run) fail the stage.
pub fn failure_terminal(e: &AppError) -> crate::runner::Terminal {
    match e.kind {
        Kind::Integrity | Kind::Internal => crate::runner::Terminal::Halted,
        _ => crate::runner::Terminal::Failed,
    }
}
