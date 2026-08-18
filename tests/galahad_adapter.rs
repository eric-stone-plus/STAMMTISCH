//! GALAHAD product adapter: real `galahad-futures` paper entry, not DoctrineFake.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde_json::json;

use stammtisch::adapters::{self, PollState, StageContext, Verdict};
use stammtisch::pipeline::{self, Stage};

const REPO: &str = env!("CARGO_MANIFEST_DIR");

fn galahad_futures() -> PathBuf {
    Path::new(REPO).join("../GALAHAD/galahad-futures")
}

fn galahad_python() -> PathBuf {
    Path::new(REPO).join(".venv/bin/python")
}

fn galahad_available() -> bool {
    galahad_futures().join("scripts/run_paper.py").is_file()
        && (galahad_python().is_file() || which_python3())
}

fn which_python3() -> bool {
    std::env::var_os("PATH")
        .map(|p| std::env::split_paths(&p).any(|d| d.join("python3").is_file()))
        .unwrap_or(false)
}

fn tmp(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "stammtisch-galahad-{tag}-{}",
        stammtisch::ids::uuid_v7().unwrap()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn stage_spec(workdir: &Path) -> Stage {
    let v = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": "t-galahad",
        "doctrine": {"pack": "galahad"},
        "stages": [{
            "id": "brief",
            "product": "galahad",
            "workdir": workdir.to_str().unwrap(),
            "out": ["galahad.summary.json"],
            "gate": "galahad_paper_go"
        }]
    });
    pipeline::validate(&v, Path::new("x.json"))
        .unwrap()
        .stages
        .into_iter()
        .next()
        .unwrap()
}

fn empty_pack() -> (PathBuf, stammtisch::doctrine::DoctrinePack) {
    let dir = tmp("pack");
    std::fs::create_dir_all(dir.join("briefs")).unwrap();
    std::fs::write(
        dir.join("doctrine.json"),
        r#"{"pack":"galahad","version":"0.1.0"}"#,
    )
    .unwrap();
    std::fs::write(dir.join("gates.json"), r#"{"gates":[]}"#).unwrap();
    std::fs::write(
        dir.join("briefs").join("brief.template.json"),
        r#"{"schema":"galahad.brief.v0","pipeline":"{{pipeline_id}}","run_id":"{{run_id}}","pack_sha256":"{{pack_sha256}}","objectives":["x"]}"#,
    )
    .unwrap();
    let pack = stammtisch::doctrine::load_dir(&dir).unwrap();
    (dir, pack)
}

#[test]
fn for_stage_galahad_is_product_not_doctrine_fake() {
    let spec = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": "t-sel",
        "doctrine": {"pack": "galahad"},
        "stages": [{"id": "brief", "product": "galahad"}]
    });
    let p = pipeline::validate(&spec, Path::new("x.json")).unwrap();
    let err = match adapters::for_stage(&p.stages[0]) {
        Ok(_) => panic!("default galahad path must be the product adapter, not DoctrineFake"),
        Err(e) => e,
    };
    assert_eq!(err.code, "galahad_workdir_required", "{err}");
}

#[test]
fn galahad_paper_fixture_collects_identity_and_targets() {
    if !galahad_available() {
        eprintln!("skip galahad_paper_fixture: galahad-futures or python missing");
        return;
    }
    if galahad_python().is_file() {
        std::env::set_var("GALAHAD_PYTHON", galahad_python());
    }
    let (_pack_dir, pack) = empty_pack();
    let run_dir = tmp("run");
    std::fs::create_dir_all(run_dir.join("artifacts")).unwrap();
    let stage = stage_spec(&galahad_futures());
    let inputs = BTreeMap::new();
    let ctx = StageContext {
        run_id: "019b4e5a-2c3d-7e8f-9a0b-1c2d3e4f5a6b",
        pipeline_id: "t-galahad",
        stage: &stage,
        doctrine: &pack,
        inputs: &inputs,
        run_dir: &run_dir,
    };
    let adapter = adapters::for_stage(&stage).unwrap();
    adapter.preflight(&ctx).unwrap();
    let handle = adapter.invoke(&ctx).unwrap();
    assert!(matches!(adapter.poll(&handle), PollState::Completed));
    let collected = adapter.collect(&handle, &ctx).unwrap();
    assert_eq!(collected.artifacts[0].0, "galahad.summary.json");
    let art = &collected.artifacts[0].1;
    assert!(art.get("run_id").and_then(|v| v.as_str()).is_some());
    assert!(art.get("as_of").and_then(|v| v.as_str()).is_some());
    assert_eq!(art["symbol"], "BTCUSDT");
    assert_eq!(art["mode"], "paper");
    assert_eq!(art["source_used"], "fixture");
    assert!(art.get("selection").is_some());
    assert!(art.get("targets").is_some());
    match art["verdict"].as_str() {
        Some("GO") => {
            assert!(matches!(collected.verdict, Verdict::Proceed));
            assert!(
                art["targets"]
                    .as_object()
                    .map(|o| !o.is_empty())
                    .unwrap_or(false)
                    || art["n_fills"].as_u64().unwrap_or(0) > 0,
                "GO session must carry targets or fills: {art}"
            );
        }
        Some("NO-GO") => {
            assert!(matches!(collected.verdict, Verdict::Refused(ref s) if s == "NO-GO"))
        }
        other => panic!("unexpected verdict {other:?}"),
    }
    stammtisch::contracts::validate_receipt(
        stammtisch::canon::canonical(&collected.receipts[0]).as_bytes(),
    )
    .unwrap();
}

#[test]
fn galahad_missing_session_fail_closes() {
    let err = stammtisch::adapters::galahad::map_paper_session(None, None).unwrap_err();
    assert_eq!(err.code, "galahad_session_missing");
}
