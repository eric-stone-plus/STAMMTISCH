//! Real-A2A integration test — **env-gated and skipped by default**.
//!
//! Runs only when ALL of the following are set:
//!   STAMMTISCH_IT=1         opt-in to integration tests against live products
//!   STAMMTISCH_A2A_URL      JSON-RPC endpoint of a real A2A v1.0 agent
//!   STAMMTISCH_A2A_CARD_URL optional Agent Card URL (defaults to
//!                           <STAMMTISCH_A2A_URL>.well-known/agent-card.json)
//!   STAMMTISCH_A2A_TOKEN    optional bearer token (declared as token_env
//!                           only when set)
//!
//! The test issues ONLY the preflight path (Agent Card discovery and the
//! binding receipt): it never sends a message or creates a task on the
//! real agent. It verifies the invocation binding end to end — the
//! receipt's host endpoint equals the configured URL and the recorded card
//! digest equals the canonical digest of the observed card document.

use std::collections::BTreeMap;
use std::path::Path;

use serde_json::json;

use stammtisch::adapters::{self, StageContext};
use stammtisch::canon;
use stammtisch::contracts;
use stammtisch::doctrine::DoctrinePack;
use stammtisch::pipeline;

fn it_config() -> Option<(String, String)> {
    if std::env::var("STAMMTISCH_IT").as_deref() != Ok("1") {
        return None;
    }
    let url = std::env::var("STAMMTISCH_A2A_URL").ok()?;
    if url.is_empty() {
        return None;
    }
    let card_url = std::env::var("STAMMTISCH_A2A_CARD_URL")
        .unwrap_or_else(|_| format!("{url}.well-known/agent-card.json"));
    Some((url, card_url))
}

#[test]
fn real_a2a_preflight_binds_card_identity() {
    let Some((url, card_url)) = it_config() else {
        eprintln!(
            "skipped: set STAMMTISCH_IT=1 with STAMMTISCH_A2A_URL to run the \
             real-A2A integration test"
        );
        return;
    };

    let mut runtime = json!({
        "protocol": "a2a",
        "endpoint": url,
        "card_url": card_url,
    });
    if std::env::var("STAMMTISCH_A2A_TOKEN").is_ok() {
        runtime["token_env"] = json!("STAMMTISCH_A2A_TOKEN");
    }
    let spec = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": "a2a-real-it",
        "doctrine": {"pack": "galahad"},
        "stages": [
            {"id": "review", "product": "quinte", "out": ["review.result"],
             "runtime": runtime}
        ]
    });
    let p = pipeline::validate(&spec, Path::new("x.json")).unwrap();
    let stage = &p.stages[0];
    let adapter = adapters::for_stage(stage).unwrap();

    let doctrine = DoctrinePack {
        dir: Path::new(".").to_path_buf(),
        name: "galahad".to_string(),
        version: None,
        digest: format!("sha256:{}", "0".repeat(64)),
        gates: Vec::new(),
        fixtures: json!({}),
    };
    let inputs = BTreeMap::new();
    let ctx = StageContext {
        run_id: "a2a-real-it-run",
        pipeline_id: "a2a-real-it",
        stage,
        doctrine: &doctrine,
        inputs: &inputs,
        run_dir: Path::new("."),
    };
    adapter.preflight(&ctx).unwrap();

    let receipts = adapter.drain_receipts();
    assert_eq!(
        receipts.len(),
        1,
        "preflight records exactly one wire observation"
    );
    let r = &receipts[0];
    let (revision, _) = contracts::validate_receipt(canon::canonical(r).as_bytes()).unwrap();
    assert_eq!(revision, "a2a.invocation.v2");
    assert_eq!(r["operation"], "card_discovery");
    assert_eq!(r["host"]["endpoint"], url.as_str());
    assert_eq!(r["host"]["card_url"], card_url.as_str());
    // The recorded digest binds the observed card bytes: recompute it.
    assert_eq!(
        r["host"]["card_sha256"],
        canon::sha256_value_prefixed(&r["upstream"])
    );
    assert_eq!(
        r["upstream_sha256"],
        canon::sha256_value_prefixed(&r["upstream"])
    );
    assert_eq!(r["host"]["protocol_version"], "1.0");
    assert_eq!(r["host"]["agent"], r["upstream"]["name"]);
}
