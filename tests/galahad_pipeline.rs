//! GALAHAD analyst/trader stage: `stammtisch-core run` → `export` → `verify`.
//! DoctrineFake is not on this path.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde_json::{json, Value};

const BIN: &str = env!("CARGO_BIN_EXE_stammtisch-core");
const REPO: &str = env!("CARGO_MANIFEST_DIR");

fn galahad_futures() -> PathBuf {
    Path::new(REPO).join("../GALAHAD/galahad-futures")
}

fn galahad_python() -> PathBuf {
    Path::new(REPO).join(".venv/bin/python")
}

fn galahad_available() -> bool {
    galahad_futures().join("scripts/run_paper.py").is_file()
}

struct Out {
    code: i32,
    stdout: String,
    stderr: String,
}

impl Out {
    fn json(&self) -> Value {
        serde_json::from_str(&self.stdout)
            .unwrap_or_else(|e| panic!("stdout is not JSON ({e}):\n{}", self.stdout))
    }
}

fn sh(home: &Path, args: &[&str]) -> Out {
    let mut cmd = Command::new(BIN);
    cmd.args(args).env("STAMMTISCH_HOME", home);
    if galahad_python().is_file() {
        cmd.env("GALAHAD_PYTHON", galahad_python());
    }
    let out = cmd.output().expect("spawn stammtisch-core");
    Out {
        code: out.status.code().unwrap_or(-1),
        stdout: String::from_utf8_lossy(&out.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&out.stderr).into_owned(),
    }
}

fn cp_dir(src: &Path, dst: &Path) {
    std::fs::create_dir_all(dst).unwrap();
    for entry in std::fs::read_dir(src).unwrap() {
        let entry = entry.unwrap();
        let (s, d) = (entry.path(), dst.join(entry.file_name()));
        if s.is_dir() {
            cp_dir(&s, &d);
        } else {
            std::fs::copy(&s, &d).unwrap();
        }
    }
}

fn launch_once(tag: &str) -> Out {
    let home = std::env::temp_dir().join(format!(
        "stammtisch-galahad-pipe-{tag}-{}",
        stammtisch::ids::uuid_v7().unwrap()
    ));
    std::fs::create_dir_all(&home).unwrap();
    let pack = home.join("pack");
    cp_dir(&Path::new(REPO).join("doctrine/examples/galahad"), &pack);
    let spec = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": "galahad-analyst",
        "doctrine": {"pack": "galahad", "ref": pack.to_str().unwrap()},
        "stages": [{
            "id": "brief",
            "product": "galahad",
            "out": ["galahad.summary.json"],
            "gate": "galahad_paper_go",
            "on_block": "blocked",
            "workdir": galahad_futures().to_str().unwrap()
        }]
    });
    let pipe = home.join("pipeline.json");
    std::fs::write(&pipe, serde_json::to_string_pretty(&spec).unwrap()).unwrap();
    let init = sh(&home, &["init"]);
    assert_eq!(init.code, 0, "init failed: {}", init.stderr);
    let run = sh(
        &home,
        &["run", "--pipeline", pipe.to_str().unwrap(), "--json"],
    );
    assert_eq!(
        run.code, 0,
        "galahad run failed: {} / {}",
        run.stdout, run.stderr
    );
    let run_id = run.json()["data"]["run_id"].as_str().unwrap().to_string();
    assert_eq!(run.json()["data"]["terminal"], "completed");

    let bundle = home.join("bundle-out");
    let export = sh(
        &home,
        &[
            "export",
            &run_id,
            "--out",
            bundle.to_str().unwrap(),
            "--json",
        ],
    );
    assert_eq!(export.code, 0, "export failed: {}", export.stderr);
    let verify = sh(
        &home,
        &["verify", "--bundle", bundle.to_str().unwrap(), "--json"],
    );
    assert_eq!(
        verify.code, 0,
        "verify --bundle failed: {} / {}",
        verify.stdout, verify.stderr
    );

    let events =
        std::fs::read_to_string(home.join("runs").join(&run_id).join("events.jsonl")).unwrap();
    let mut digest = None;
    for line in events.lines() {
        let ev: Value = serde_json::from_str(line).unwrap();
        if ev["type"] == "stage.artifact_recorded"
            && ev["payload"]["name"] == "galahad.summary.json"
        {
            digest = ev["payload"]["digest"].as_str().map(|s| s.to_string());
        }
    }
    let digest = digest.expect("galahad.summary.json recorded");
    let hex = &digest["sha256:".len()..];
    let art: Value = serde_json::from_slice(
        &std::fs::read(home.join("runs").join(&run_id).join("artifacts").join(hex)).unwrap(),
    )
    .unwrap();
    assert!(art.get("run_id").is_some());
    assert!(art.get("selection").is_some());
    assert!(art.get("targets").is_some() || art["verdict"] == "NO-GO");
    assert_eq!(art["mode"], "paper");
    verify
}

#[test]
fn galahad_run_export_verify_twice() {
    if !galahad_available() {
        eprintln!("skip galahad_run_export_verify_twice: galahad-futures missing");
        return;
    }
    let v1 = launch_once("1");
    let v2 = launch_once("2");
    assert_eq!(v1.json()["ok"], true);
    assert_eq!(v2.json()["ok"], true);
}

#[test]
fn fake_doctrine_does_not_invoke_galahad() {
    let home = std::env::temp_dir().join(format!(
        "stammtisch-doctrine-fake-{}",
        stammtisch::ids::uuid_v7().unwrap()
    ));
    std::fs::create_dir_all(&home).unwrap();
    let pack = home.join("pack");
    cp_dir(&Path::new(REPO).join("doctrine/examples/galahad"), &pack);
    let spec = json!({
        "schema": "stammtisch.pipeline.v0",
        "id": "offline-doctrine",
        "doctrine": {"pack": "galahad", "ref": pack.to_str().unwrap()},
        "stages": [{
            "id": "brief",
            "product": "doctrine",
            "adapter": "fake",
            "out": ["brief.json"],
            "gate": "brief_schema_valid"
        }]
    });
    let pipe = home.join("pipeline.json");
    std::fs::write(&pipe, serde_json::to_string_pretty(&spec).unwrap()).unwrap();
    let init = sh(&home, &["init"]);
    assert_eq!(init.code, 0, "{}", init.stderr);
    let run = sh(
        &home,
        &["run", "--pipeline", pipe.to_str().unwrap(), "--json"],
    );
    assert_eq!(
        run.code, 0,
        "fake doctrine run failed: {} / {}",
        run.stdout, run.stderr
    );
    let run_id = run.json()["data"]["run_id"].as_str().unwrap().to_string();
    let events =
        std::fs::read_to_string(home.join("runs").join(&run_id).join("events.jsonl")).unwrap();
    assert!(
        !events.contains("galahad-paper") && !events.contains("run_paper.py"),
        "fake doctrine must not shell out to GALAHAD"
    );
    let mut digest = None;
    for line in events.lines() {
        let ev: Value = serde_json::from_str(line).unwrap();
        if ev["type"] == "stage.artifact_recorded" && ev["payload"]["name"] == "brief.json" {
            digest = ev["payload"]["digest"].as_str().map(|s| s.to_string());
        }
    }
    let hex = &digest.expect("brief.json recorded")["sha256:".len()..];
    let brief: Value = serde_json::from_slice(
        &std::fs::read(home.join("runs").join(&run_id).join("artifacts").join(hex)).unwrap(),
    )
    .unwrap();
    assert_eq!(brief["schema"], "galahad.brief.v0");
    assert!(brief.get("objectives").is_some());
    assert!(brief.get("targets").is_none());
}
