//! Pipeline spec loading: contract validation plus semantic checks
//! (architecture doc §3.1). The canonical digest of the normalized spec is
//! the run's provenance root.

use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::canon;
use crate::error::AppError;
use crate::jsonval;

#[derive(Debug, Clone)]
pub struct Stage {
    pub id: String,
    pub product: String,
    pub inputs: Vec<String>,
    pub outputs: Vec<String>,
    pub gate: Option<String>,
    /// Operator-local evidence paths rendered into the brief's
    /// ``evidence_roots``; the downstream review product snapshots them.
    pub evidence: Vec<String>,
    /// "halt" (default per pipeline schema) | "blocked"
    pub on_block: String,
    /// Per-stage wall-clock budget for real invocations (default 3600 s).
    pub timeout_seconds: u64,
    /// Interval between one-shot host status polls (default 30 s).
    pub poll_seconds: u64,
    /// Real runtime binding for products that run against a wire protocol
    /// (docs/protocol-layer.md). Absent = product CLI or offline fake.
    pub runtime: Option<Runtime>,
    /// "fake" | "product". None = product CLI for highball/galahad, fake otherwise.
    pub adapter: Option<String>,
    /// Product working directory (GALAHAD project root). HIGHBALL evidence
    /// comes exclusively from declared, content-addressed stage inputs.
    pub workdir: Option<PathBuf>,
    /// GALAHAD execution backend ("paper" | "nautilus"). None = product
    /// default (the reference paper book). Schema-validated.
    pub engine: Option<String>,
}

/// Wire runtime a real product adapter dials. Protocol-agnostic at the
/// Stage level: the adapter dispatches on `protocol` (only "a2a" today).
#[derive(Debug, Clone)]
pub struct Runtime {
    pub protocol: String,
    pub endpoint: String,
    pub card_url: Option<String>,
    pub token_env: Option<String>,
    pub card_sha256: Option<String>,
}

#[derive(Debug, Clone)]
pub struct Pipeline {
    pub id: String,
    pub doctrine_pack: String,
    pub doctrine_ref: Option<String>,
    pub stages: Vec<Stage>,
    /// Normalized spec (parsed form; objects serialize with sorted keys).
    pub value: Value,
    /// sha256:<hex> of the canonical serialization — provenance root.
    pub canonical_sha256: String,
    pub source_path: PathBuf,
}

pub fn load(path: &Path) -> Result<Pipeline, AppError> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| AppError::usage("pipeline_unreadable", format!("{}: {e}", path.display())))?;
    let value: Value = serde_json::from_str(&text)
        .map_err(|e| AppError::usage("pipeline_unparseable", format!("{}: {e}", path.display())))?;
    validate(&value, path)
}

/// Validate an already-parsed spec against the contract + semantic rules.
pub fn validate(value: &Value, source_path: &Path) -> Result<Pipeline, AppError> {
    let schema: Value =
        serde_json::from_str(crate::schemas::PIPELINE).expect("embedded schema parses");
    let errs = jsonval::violations(&schema, value);
    if !errs.is_empty() {
        return Err(AppError::usage(
            "pipeline_schema_invalid",
            format!(
                "pipeline spec violates stammtisch.pipeline.v0: {}",
                errs.join("; ")
            ),
        ));
    }

    let id = value["id"].as_str().expect("schema-checked").to_string();
    let doctrine = &value["doctrine"];
    let doctrine_pack = doctrine["pack"]
        .as_str()
        .expect("schema-checked")
        .to_string();
    let doctrine_ref = doctrine
        .get("ref")
        .and_then(Value::as_str)
        .map(str::to_string);

    let mut stages = Vec::new();
    let mut seen_ids: Vec<&str> = Vec::new();
    let mut available: Vec<String> = Vec::new(); // artifact names produced so far
    for s in value["stages"].as_array().expect("schema-checked") {
        let sid = s["id"].as_str().expect("schema-checked");
        if seen_ids.contains(&sid) {
            return Err(AppError::usage(
                "pipeline_stage_duplicate",
                format!("duplicate stage id '{sid}'"),
            ));
        }
        seen_ids.push(sid);
        let inputs: Vec<String> = s
            .get("in")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        for input in &inputs {
            if !available.contains(input) {
                return Err(AppError::usage(
                    "pipeline_input_unresolved",
                    format!(
                        "stage '{sid}' consumes '{input}' but no earlier stage produces it \
                         (cycles and forward references are rejected)"
                    ),
                ));
            }
        }
        let outputs: Vec<String> = s
            .get("out")
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default();
        available.extend(outputs.iter().cloned());
        let product = s["product"].as_str().expect("schema-checked").to_string();
        if product == "highball"
            && s.get("runtime").is_none()
            && s.get("adapter").and_then(Value::as_str) != Some("fake")
        {
            for required in [
                crate::adapters::highball::ROUTE_REQUEST_INPUT,
                crate::adapters::highball::RESIDUAL_TRACE_INPUT,
            ] {
                if !inputs.iter().any(|name| name == required) {
                    return Err(AppError::usage(
                        "highball_input_required",
                        format!(
                            "stage '{sid}' must declare typed upstream input '{required}'; \
                             HIGHBALL never reads ambient workdir evidence"
                        ),
                    ));
                }
            }
        }
        let runtime = s.get("runtime").map(|r| Runtime {
            protocol: r["protocol"].as_str().expect("schema-checked").to_string(),
            endpoint: r["endpoint"].as_str().expect("schema-checked").to_string(),
            card_url: r
                .get("card_url")
                .and_then(Value::as_str)
                .map(str::to_string),
            token_env: r
                .get("token_env")
                .and_then(Value::as_str)
                .map(str::to_string),
            card_sha256: r
                .get("card_sha256")
                .and_then(Value::as_str)
                .map(str::to_string),
        });
        // Fail closed at spec time: a product with no offline fake must
        // declare its runtime in the pipeline (no silent fallback, no
        // ambient environment selection).
        if product == "quinte" && runtime.is_none() {
            return Err(AppError::usage(
                "quinte_runtime_required",
                format!(
                    "stage '{sid}' declares product 'quinte' without a runtime; \
                     quinte has no offline fake — set stage.runtime to a real \
                     A2A binding (docs/protocol-layer.md)"
                ),
            ));
        }
        stages.push(Stage {
            id: sid.to_string(),
            product,
            inputs,
            outputs,
            gate: s.get("gate").and_then(Value::as_str).map(str::to_string),
            evidence: s
                .get("evidence")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|v| v.as_str().map(str::to_string))
                        .collect()
                })
                .unwrap_or_default(),
            on_block: s
                .get("on_block")
                .and_then(Value::as_str)
                .unwrap_or("halt")
                .to_string(),
            timeout_seconds: s
                .get("timeout_seconds")
                .and_then(Value::as_u64)
                .unwrap_or(3600),
            poll_seconds: s.get("poll_seconds").and_then(Value::as_u64).unwrap_or(30),
            runtime,
            adapter: s.get("adapter").and_then(Value::as_str).map(str::to_string),
            workdir: s.get("workdir").and_then(Value::as_str).map(PathBuf::from),
            engine: s.get("engine").and_then(Value::as_str).map(str::to_string),
        });
    }

    Ok(Pipeline {
        id,
        doctrine_pack,
        doctrine_ref,
        stages,
        canonical_sha256: canon::sha256_value_prefixed(value),
        value: value.clone(),
        source_path: source_path.to_path_buf(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::PathBuf;

    fn spec() -> Value {
        json!({
            "schema": "stammtisch.pipeline.v0",
            "id": "t-pipe",
            "doctrine": {"pack": "galahad"},
            "stages": [
                {"id": "brief", "product": "doctrine", "out": ["brief.json"]},
                {"id": "deliver", "product": "highball", "adapter": "fake", "in": ["brief.json"],
                 "out": ["deliver.packet.json"], "gate": "g1", "on_block": "blocked"}
            ]
        })
    }

    #[test]
    fn valid_spec_loads() {
        let p = validate(&spec(), &PathBuf::from("x.json")).unwrap();
        assert_eq!(p.id, "t-pipe");
        assert_eq!(p.stages.len(), 2);
        assert_eq!(p.stages[1].on_block, "blocked");
        assert_eq!(p.stages[0].on_block, "halt"); // schema default
        assert!(p.canonical_sha256.starts_with("sha256:"));
    }

    #[test]
    fn forward_reference_rejected() {
        let mut v = spec();
        v["stages"][1]["in"] = json!(["deliver.packet.json"]);
        let e = validate(&v, &PathBuf::from("x.json")).unwrap_err();
        assert_eq!(e.code, "pipeline_input_unresolved");
    }

    #[test]
    fn duplicate_stage_rejected() {
        let mut v = spec();
        v["stages"][1]["id"] = json!("brief");
        let e = validate(&v, &PathBuf::from("x.json")).unwrap_err();
        assert_eq!(e.code, "pipeline_stage_duplicate");
    }

    #[test]
    fn schema_violation_rejected() {
        let mut v = spec();
        v.as_object_mut().unwrap().remove("doctrine");
        let e = validate(&v, &PathBuf::from("x.json")).unwrap_err();
        assert_eq!(e.code, "pipeline_schema_invalid");
    }

    #[test]
    fn stage_fields_parse_with_defaults() {
        let p = validate(&spec(), &PathBuf::from("x.json")).unwrap();
        let deliver = &p.stages[1];
        assert_eq!(deliver.timeout_seconds, 3600);
        assert_eq!(deliver.poll_seconds, 30);

        let mut v = spec();
        v["stages"][1]["timeout_seconds"] = json!(120);
        v["stages"][1]["poll_seconds"] = json!(5);
        let p = validate(&v, &PathBuf::from("x.json")).unwrap();
        let deliver = &p.stages[1];
        assert_eq!(deliver.timeout_seconds, 120);
        assert_eq!(deliver.poll_seconds, 5);
    }

    #[test]
    fn canonical_digest_stable() {
        let a = validate(&spec(), &PathBuf::from("x.json")).unwrap();
        let b = validate(&spec(), &PathBuf::from("y.json")).unwrap();
        assert_eq!(a.canonical_sha256, b.canonical_sha256);
    }

    #[test]
    fn quinte_requires_runtime() {
        let mut v = spec();
        v["stages"][1]["product"] = json!("quinte");
        let e = validate(&v, &PathBuf::from("x.json")).unwrap_err();
        assert_eq!(e.code, "quinte_runtime_required");

        v["stages"][1]["runtime"] = json!({
            "protocol": "a2a",
            "endpoint": "https://a2a.example.invalid/",
            "token_env": "A2A_TOKEN",
            "card_sha256": format!("sha256:{}", "3".repeat(64)),
        });
        let p = validate(&v, &PathBuf::from("x.json")).unwrap();
        let rt = p.stages[1].runtime.as_ref().unwrap();
        assert_eq!(rt.protocol, "a2a");
        assert_eq!(rt.endpoint, "https://a2a.example.invalid/");
        assert_eq!(rt.token_env.as_deref(), Some("A2A_TOKEN"));
        assert_eq!(rt.card_url, None); // defaults at adapter construction
        assert!(rt.card_sha256.as_deref().unwrap().starts_with("sha256:"));
    }

    #[test]
    fn runtime_allows_any_known_product() {
        // A fake-backed product may also declare a real runtime; the runner
        // then selects the wire adapter over the offline fake.
        let mut v = spec();
        v["stages"][1]["runtime"] = json!({
            "protocol": "a2a",
            "endpoint": "https://a2a.example.invalid/"
        });
        let p = validate(&v, &PathBuf::from("x.json")).unwrap();
        assert!(p.stages[1].runtime.is_some());
    }
}
