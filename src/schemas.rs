//! The five STAMMTISCH contract schemas, embedded at compile time so the
//! single static binary is self-contained (validate/verify work without the
//! repository present). `schemas/` in the repository remains the source of
//! truth — these paths are compile-time includes of exactly those files.

pub const PIPELINE: &str = include_str!("../schemas/pipeline.schema.json");
pub const RUN_MANIFEST: &str = include_str!("../schemas/run-manifest.schema.json");
pub const RUN_MANIFEST_V1: &str = include_str!("../schemas/run-manifest-v1.schema.json");
pub const BUNDLE_MANIFEST: &str = include_str!("../schemas/bundle-manifest.schema.json");
pub const GATE_RECORD: &str = include_str!("../schemas/gate-record.schema.json");
pub const RUN_EVENT: &str = include_str!("../schemas/run-event.schema.json");
pub const A2A_INVOCATION: &str = include_str!("../schemas/a2a-invocation.schema.json");
pub const A2A_INVOCATION_V2: &str = include_str!("../schemas/a2a-invocation-v2.schema.json");
pub const COST_LEDGER: &str = include_str!("../schemas/cost-ledger.schema.json");

#[cfg(test)]
mod tests {
    #[test]
    fn all_schemas_parse_as_json() {
        for (name, text) in [
            ("pipeline", super::PIPELINE),
            ("run-manifest", super::RUN_MANIFEST),
            ("run-manifest-v1", super::RUN_MANIFEST_V1),
            ("bundle-manifest", super::BUNDLE_MANIFEST),
            ("gate-record", super::GATE_RECORD),
            ("run-event", super::RUN_EVENT),
            ("a2a-invocation", super::A2A_INVOCATION),
            ("a2a-invocation-v2", super::A2A_INVOCATION_V2),
            ("cost-ledger", super::COST_LEDGER),
        ] {
            serde_json::from_str::<serde_json::Value>(text)
                .unwrap_or_else(|e| panic!("{name} schema must parse: {e}"));
        }
    }
}
