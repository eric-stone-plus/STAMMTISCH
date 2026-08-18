//! Fake product adapters (P0): each fake implements the
//! preflight/invoke/poll/collect contract but is driven synchronously by
//! the runner — no real processes, no blocking waits. Receipts and
//! artifacts are contract-accurate canned documents.
//! Fixture directives in the doctrine pack (`doctrine.json` → `fixtures`)
//! steer the fakes so the refusal path is testable end to end:
//!
//! ```json
//! "fixtures": {
//!   "highball": {"decision": "DENIED",
//!                "revision": "highball.action-packet.v9"}
//! }
//! ```
//!
//! A `revision` override makes the fake emit an unknown contract revision —
//! the runner must halt, never parse best-effort (conformance item 10).

use serde_json::{json, Value};

use super::{Adapter, Collected, InvocationHandle, PollState, StageContext, Verdict};
use crate::canon;
use crate::error::AppError;

fn ok_handle(product: &str, ctx: &StageContext) -> Result<InvocationHandle, AppError> {
    Ok(InvocationHandle {
        id: format!("fake-{product}-{}", &ctx.run_id[..8.min(ctx.run_id.len())]),
        product: product.to_string(),
    })
}

/// Build the brief's ``evidence_roots`` array from the stage's declared
/// evidence paths. Every path must exist at render time — a brief that
/// points the review product at missing evidence fails closed here instead
/// of shipping a review over nothing.
fn evidence_roots(ctx: &StageContext) -> Result<Value, AppError> {
    let mut roots: Vec<Value> = Vec::new();
    for raw in &ctx.stage.evidence {
        let path = std::path::Path::new(raw);
        if !path.exists() {
            return Err(AppError::product(
                "adapter_invoke_failed",
                format!(
                    "stage '{}' declares evidence '{}' that does not exist",
                    ctx.stage.id, raw
                ),
            ));
        }
        roots.push(Value::String(raw.clone()));
    }
    Ok(Value::Array(roots))
}

// ---------------------------------------------------------------- doctrine

pub struct DoctrineFake;

impl Adapter for DoctrineFake {
    fn preflight(&self, ctx: &StageContext) -> Result<(), AppError> {
        let template = ctx.doctrine.dir.join("briefs").join("brief.template.json");
        if !template.is_file() {
            return Err(AppError::product(
                "adapter_preflight_failed",
                format!("doctrine pack lacks brief template {}", template.display()),
            ));
        }
        Ok(())
    }

    fn invoke(&self, ctx: &StageContext) -> Result<InvocationHandle, AppError> {
        ok_handle("doctrine", ctx)
    }

    fn poll(&self, _h: &InvocationHandle) -> PollState {
        PollState::Completed
    }

    fn collect(&self, _h: &InvocationHandle, ctx: &StageContext) -> Result<Collected, AppError> {
        let template_path = ctx.doctrine.dir.join("briefs").join("brief.template.json");
        let template = std::fs::read_to_string(&template_path).map_err(|e| {
            AppError::product("adapter_invoke_failed", format!("read brief template: {e}"))
        })?;
        let rendered = template
            .replace("{{pipeline_id}}", ctx.pipeline_id)
            .replace("{{run_id}}", ctx.run_id)
            .replace("{{pack_sha256}}", &ctx.doctrine.digest);
        let mut brief: Value = serde_json::from_str(&rendered).map_err(|e| {
            AppError::product(
                "adapter_invoke_failed",
                format!("brief template renders to invalid JSON: {e}"),
            )
        })?;
        if !ctx.stage.evidence.is_empty() {
            // The evidence_roots array is inserted after rendering (never a
            // template placeholder) so the template itself stays valid JSON
            // for tooling that parses it directly.
            brief["evidence_roots"] = evidence_roots(ctx)?;
        }
        let brief_digest = canon::sha256_value_prefixed(&brief);
        let receipt = json!({
            "schema": "doctrine.brief.v0",
            "pack": ctx.doctrine.name,
            "pack_version": ctx.doctrine.version,
            "pack_sha256": ctx.doctrine.digest,
            "brief_sha256": brief_digest,
        });
        Ok(Collected {
            receipts: vec![receipt],
            artifacts: vec![("brief.json".to_string(), brief)],
            verdict: Verdict::Proceed,
        })
    }
}

// ---------------------------------------------------------------- highball

pub struct HighballFake;

impl Adapter for HighballFake {
    fn preflight(&self, _ctx: &StageContext) -> Result<(), AppError> {
        Ok(())
    }

    fn invoke(&self, ctx: &StageContext) -> Result<InvocationHandle, AppError> {
        ok_handle("highball", ctx)
    }

    fn poll(&self, _h: &InvocationHandle) -> PollState {
        PollState::Completed
    }

    fn collect(&self, _h: &InvocationHandle, ctx: &StageContext) -> Result<Collected, AppError> {
        let subject_ref = ctx
            .inputs
            .get("review.result")
            .or_else(|| ctx.inputs.get("brief.json"))
            .cloned()
            .unwrap_or_else(|| format!("sha256:{}", "0".repeat(64)));
        let packet_id = format!("pkt-{}", ctx.run_id);
        let decision = ctx
            .doctrine
            .fixture("highball", "decision")
            .unwrap_or("AUTHORIZED");
        let action_decision = if decision == "AUTHORIZED" {
            "pass"
        } else {
            "block"
        };
        let packet = json!({
            "schema": "highball.delivery-plan.v0",
            "packet_id": packet_id,
            "route": "direct-evidence",
            "subject_sha256": subject_ref,
            "action_decision": action_decision,
            "actions": [
                {"kind": "deliver-report", "target": "evidence-bundle", "authorized": decision == "AUTHORIZED"}
            ]
        });
        let revision = ctx
            .doctrine
            .fixture("highball", "revision")
            .unwrap_or("highball.action-packet.v1");
        let receipt = json!({
            "schema": revision,
            "packet_id": packet_id,
            "route": "direct-evidence",
            "decision": decision,
            "action_decision": action_decision,
            "packet_sha256": canon::sha256_value_prefixed(&packet),
            "reasons": [],
        });
        let out_verdict = if decision == "AUTHORIZED" {
            Verdict::Proceed
        } else {
            Verdict::Refused("DENIED".to_string())
        };
        Ok(Collected {
            receipts: vec![receipt],
            artifacts: vec![("deliver.packet.json".to_string(), packet)],
            verdict: out_verdict,
        })
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeMap;

    use super::*;
    use crate::adapters::for_product;
    use crate::doctrine::DoctrinePack;
    use crate::pipeline::{self, Stage};

    fn ctx_fixture<'a>(
        run_id: &'a str,
        stage: &'a Stage,
        doctrine: &'a DoctrinePack,
        inputs: &'a BTreeMap<String, String>,
    ) -> StageContext<'a> {
        StageContext {
            run_id,
            pipeline_id: "t-pipe",
            stage,
            doctrine,
            inputs,
            // Fakes never touch the run directory.
            run_dir: std::path::Path::new("/nonexistent-stammtisch-fake-run-dir"),
        }
    }

    fn pack_with_fixtures(fixtures: Value) -> (std::path::PathBuf, DoctrinePack) {
        let dir = std::env::temp_dir().join(format!(
            "stammtisch-adapter-{}",
            crate::ids::uuid_v7().unwrap()
        ));
        std::fs::create_dir_all(dir.join("briefs")).unwrap();
        std::fs::write(
            dir.join("doctrine.json"),
            serde_json::to_string(&json!({
                "pack": "galahad", "version": "0.1.0", "fixtures": fixtures
            }))
            .unwrap(),
        )
        .unwrap();
        std::fs::write(dir.join("gates.json"), r#"{"gates":[]}"#).unwrap();
        std::fs::write(
            dir.join("briefs").join("brief.template.json"),
            r#"{"schema":"galahad.brief.v0","pipeline":"{{pipeline_id}}","run_id":"{{run_id}}","pack_sha256":"{{pack_sha256}}","objectives":["x"]}"#,
        )
        .unwrap();
        let pack = crate::doctrine::load_dir(&dir).unwrap();
        (dir, pack)
    }

    fn stage(id: &str, product: &str) -> Stage {
        let v = json!({
            "schema": "stammtisch.pipeline.v0", "id": "t-pipe",
            "doctrine": {"pack": "galahad"},
            "stages": [{"id": id, "product": product, "adapter": "fake"}]
        });
        pipeline::validate(&v, std::path::Path::new("x.json"))
            .unwrap()
            .stages
            .into_iter()
            .next()
            .unwrap()
    }

    #[test]
    fn doctrine_fake_materializes_brief() {
        let (dir, pack) = pack_with_fixtures(json!({}));
        let s = stage("brief", "doctrine");
        let inputs = BTreeMap::new();
        let ctx = ctx_fixture("run-1", &s, &pack, &inputs);
        let a = for_product("doctrine").unwrap();
        a.preflight(&ctx).unwrap();
        let h = a.invoke(&ctx).unwrap();
        assert!(matches!(a.poll(&h), PollState::Completed));
        let c = a.collect(&h, &ctx).unwrap();
        assert_eq!(c.receipts.len(), 1);
        let (rev, _) =
            crate::contracts::validate_receipt(crate::canon::canonical(&c.receipts[0]).as_bytes())
                .unwrap();
        assert_eq!(rev, "doctrine.brief.v0");
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn doctrine_brief_carries_evidence_roots_only_when_declared() {
        let (dir, pack) = pack_with_fixtures(json!({}));
        // Stage without evidence: the brief has no evidence_roots field.
        let s = stage("brief", "doctrine");
        let empty1 = BTreeMap::new();
        let ctx = ctx_fixture("run-1", &s, &pack, &empty1);
        let a = for_product("doctrine").unwrap();
        a.preflight(&ctx).unwrap();
        let h = a.invoke(&ctx).unwrap();
        let c = a.collect(&h, &ctx).unwrap();
        let brief = &c.artifacts[0].1;
        assert!(brief.get("evidence_roots").is_none());

        // Stage with an existing evidence path: rendered into evidence_roots.
        let evidence = dir.join("evidence.json");
        std::fs::write(&evidence, b"{}").unwrap();
        let v = json!({
            "schema": "stammtisch.pipeline.v0", "id": "t-pipe",
            "doctrine": {"pack": "galahad"},
            "stages": [{"id": "brief", "product": "doctrine", "adapter": "fake",
                        "evidence": [evidence.to_str().unwrap()]}]
        });
        let s2 = pipeline::validate(&v, std::path::Path::new("x.json"))
            .unwrap()
            .stages
            .into_iter()
            .next()
            .unwrap();
        let empty2 = BTreeMap::new();
        let ctx2 = ctx_fixture("run-2", &s2, &pack, &empty2);
        let c2 = a.collect(&a.invoke(&ctx2).unwrap(), &ctx2).unwrap();
        let roots = c2.artifacts[0].1["evidence_roots"].as_array().unwrap();
        assert_eq!(roots.len(), 1);
        assert_eq!(roots[0].as_str().unwrap(), evidence.to_str().unwrap());

        // Missing evidence path fails closed before any brief ships.
        let v3 = json!({
            "schema": "stammtisch.pipeline.v0", "id": "t-pipe",
            "doctrine": {"pack": "galahad"},
            "stages": [{"id": "brief", "product": "doctrine", "adapter": "fake",
                        "evidence": ["/nonexistent-stammtisch-evidence"]}]
        });
        let s3 = pipeline::validate(&v3, std::path::Path::new("x.json"))
            .unwrap()
            .stages
            .into_iter()
            .next()
            .unwrap();
        let empty3 = BTreeMap::new();
        let ctx3 = ctx_fixture("run-3", &s3, &pack, &empty3);
        let outcome = a.collect(&a.invoke(&ctx3).unwrap(), &ctx3);
        assert!(matches!(outcome, Err(ref e) if e.to_string().contains("does not exist")));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn highball_fake_authorized_and_denied_and_drift() {
        let inputs: BTreeMap<String, String> = [(
            "brief.json".to_string(),
            format!("sha256:{}", "1".repeat(64)),
        )]
        .into_iter()
        .collect();

        let (dir, pack) = pack_with_fixtures(json!({"highball": {"decision": "AUTHORIZED"}}));
        let s = stage("deliver", "highball");
        let ctx = ctx_fixture("run-1", &s, &pack, &inputs);
        let a = for_product("highball").unwrap();
        let h = a.invoke(&ctx).unwrap();
        let c = a.collect(&h, &ctx).unwrap();
        assert!(matches!(c.verdict, Verdict::Proceed));
        crate::contracts::validate_receipt(crate::canon::canonical(&c.receipts[0]).as_bytes())
            .unwrap();
        std::fs::remove_dir_all(&dir).ok();

        let (dir, pack) = pack_with_fixtures(json!({"highball": {"decision": "DENIED"}}));
        let ctx = ctx_fixture("run-1", &s, &pack, &inputs);
        let a = for_product("highball").unwrap();
        let h = a.invoke(&ctx).unwrap();
        let c = a.collect(&h, &ctx).unwrap();
        assert!(matches!(c.verdict, Verdict::Refused(ref v) if v == "DENIED"));
        // A DENIED receipt is still contract-valid evidence.
        crate::contracts::validate_receipt(crate::canon::canonical(&c.receipts[0]).as_bytes())
            .unwrap();
        std::fs::remove_dir_all(&dir).ok();

        // Contract drift: unknown revision is emitted, and validation must
        // reject it (the runner turns this into a halt).
        let (dir, pack) =
            pack_with_fixtures(json!({"highball": {"revision": "highball.action-packet.v9"}}));
        let ctx = ctx_fixture("run-1", &s, &pack, &inputs);
        let a = for_product("highball").unwrap();
        let h = a.invoke(&ctx).unwrap();
        let c = a.collect(&h, &ctx).unwrap();
        let err =
            crate::contracts::validate_receipt(crate::canon::canonical(&c.receipts[0]).as_bytes())
                .unwrap_err();
        assert!(matches!(
            err,
            crate::contracts::ReceiptError::UnknownRevision(_)
        ));
        std::fs::remove_dir_all(&dir).ok();
    }
}
