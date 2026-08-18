//! Doctrine pack resolution and digest (architecture doc §5.2).
//!
//! A pack is a versioned directory: `doctrine.json` (identity + fake-adapter
//! fixture directives for P0), `gates.json` (gate definitions), plus brief
//! templates and domain schemas. The pack digest covers every file under
//! the directory; doctrine changes between runs surface as digest drift in
//! provenance, and a pinned `ref: "sha256:..."` turns drift into a hard
//! contract error.
//!
//! `ref` resolution order:
//!   1. absolute path → use directly
//!   2. relative path → resolved against the pipeline file's directory
//!   3. "sha256:<hex>" or absent → name search in
//!      `<pipeline_dir>/doctrine/<pack>` and `<state_root>/doctrine/<pack>`;
//!      a digest ref must then match the computed digest exactly.

use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::canon;
use crate::error::AppError;

#[derive(Debug, Clone)]
pub struct GateDefEntry {
    pub id: String,
    pub raw: Value,
}

#[derive(Debug, Clone)]
pub struct DoctrinePack {
    pub dir: PathBuf,
    pub name: String,
    pub version: Option<String>,
    /// sha256:<hex> over the sorted (relpath, file-digest) list.
    pub digest: String,
    pub gates: Vec<GateDefEntry>,
    /// P0 fake-adapter directives, e.g. {"highball": {"decision": "DENIED"}}.
    pub fixtures: Value,
}

pub fn resolve(
    pipeline: &crate::pipeline::Pipeline,
    state_root: &Path,
) -> Result<DoctrinePack, AppError> {
    let pipeline_dir = pipeline
        .source_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));

    let (dir, pinned_digest) = match pipeline.doctrine_ref.as_deref() {
        Some(r) if r.starts_with("sha256:") => (
            name_search(&pipeline_dir, state_root, &pipeline.doctrine_pack)?,
            Some(r.to_string()),
        ),
        Some(r) => {
            let p = PathBuf::from(r);
            let dir = if p.is_absolute() {
                p
            } else {
                pipeline_dir.join(p)
            };
            if !dir.is_dir() {
                return Err(AppError::usage(
                    "doctrine_not_found",
                    format!(
                        "doctrine ref '{}' resolves to missing dir {}",
                        r,
                        dir.display()
                    ),
                ));
            }
            (dir, None)
        }
        None => (
            name_search(&pipeline_dir, state_root, &pipeline.doctrine_pack)?,
            None,
        ),
    };

    let pack = load_dir(&dir)?;

    if pack.name != pipeline.doctrine_pack {
        return Err(AppError::usage(
            "doctrine_pack_mismatch",
            format!(
                "pipeline wants pack '{}' but {} contains pack '{}'",
                pipeline.doctrine_pack,
                dir.display(),
                pack.name
            ),
        ));
    }
    if let Some(want) = pinned_digest {
        if want != pack.digest {
            return Err(AppError::usage(
                "doctrine_digest_drift",
                format!(
                    "doctrine ref pins {want} but pack {} digests to {}",
                    dir.display(),
                    pack.digest
                ),
            ));
        }
    }
    Ok(pack)
}

fn name_search(pipeline_dir: &Path, state_root: &Path, pack: &str) -> Result<PathBuf, AppError> {
    for cand in [
        pipeline_dir.join("doctrine").join(pack),
        state_root.join("doctrine").join(pack),
    ] {
        if cand.is_dir() {
            return Ok(cand);
        }
    }
    Err(AppError::usage(
        "doctrine_not_found",
        format!(
            "doctrine pack '{pack}' not found (looked in {} and {}); \
             set doctrine.ref to a path",
            pipeline_dir.join("doctrine").display(),
            state_root.join("doctrine").display()
        ),
    ))
}

pub fn load_dir(dir: &Path) -> Result<DoctrinePack, AppError> {
    let doctrine_json_path = dir.join("doctrine.json");
    let doctrine_text = std::fs::read_to_string(&doctrine_json_path).map_err(|e| {
        AppError::usage(
            "doctrine_unreadable",
            format!("{}: {e}", doctrine_json_path.display()),
        )
    })?;
    let doctrine: Value = serde_json::from_str(&doctrine_text).map_err(|e| {
        AppError::usage(
            "doctrine_unparseable",
            format!("{}: {e}", doctrine_json_path.display()),
        )
    })?;
    let name = doctrine
        .get("pack")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            AppError::usage(
                "doctrine_missing_pack_name",
                format!("{} has no \"pack\" field", doctrine_json_path.display()),
            )
        })?
        .to_string();
    let version = doctrine
        .get("version")
        .and_then(Value::as_str)
        .map(str::to_string);
    let fixtures = doctrine
        .get("fixtures")
        .cloned()
        .unwrap_or_else(|| Value::Object(Default::default()));

    let gates_path = dir.join("gates.json");
    let gates_text = std::fs::read_to_string(&gates_path).map_err(|e| {
        AppError::usage(
            "doctrine_unreadable",
            format!("{}: {e}", gates_path.display()),
        )
    })?;
    let gates_doc: Value = serde_json::from_str(&gates_text).map_err(|e| {
        AppError::usage(
            "doctrine_unparseable",
            format!("{}: {e}", gates_path.display()),
        )
    })?;
    let mut gates = Vec::new();
    for g in gates_doc
        .get("gates")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            AppError::usage(
                "doctrine_gates_invalid",
                format!("{} lacks a \"gates\" array", gates_path.display()),
            )
        })?
    {
        let id = g.get("id").and_then(Value::as_str).ok_or_else(|| {
            AppError::usage(
                "doctrine_gates_invalid",
                format!("gate without id in {}", gates_path.display()),
            )
        })?;
        gates.push(GateDefEntry {
            id: id.to_string(),
            raw: g.clone(),
        });
    }

    let digest = pack_digest(dir)?;
    Ok(DoctrinePack {
        dir: dir.to_path_buf(),
        name,
        version,
        digest,
        gates,
        fixtures,
    })
}

/// Digest over the whole pack: sorted "relpath:sha256hex" lines, hashed.
pub fn pack_digest(dir: &Path) -> Result<String, AppError> {
    let mut files = Vec::new();
    collect_files(dir, dir, &mut files)?;
    files.sort();
    let mut listing = String::new();
    for rel in files {
        let bytes = std::fs::read(dir.join(&rel))?;
        listing.push_str(&rel);
        listing.push(':');
        listing.push_str(&canon::sha256_hex(&bytes));
        listing.push('\n');
    }
    Ok(canon::sha256_prefixed(listing.as_bytes()))
}

/// All files under the pack, as `/`-separated relative paths.
pub fn pack_files(dir: &Path) -> Result<Vec<String>, AppError> {
    let mut files = Vec::new();
    collect_files(dir, dir, &mut files)?;
    files.sort();
    Ok(files)
}

fn collect_files(base: &Path, dir: &Path, out: &mut Vec<String>) -> Result<(), AppError> {
    let entries = std::fs::read_dir(dir)
        .map_err(|e| AppError::usage("doctrine_unreadable", format!("{}: {e}", dir.display())))?;
    for entry in entries {
        let path = entry?.path();
        if path.is_dir() {
            collect_files(base, &path, out)?;
        } else if path.is_file() {
            let rel = path
                .strip_prefix(base)
                .map_err(|e| AppError::internal(format!("relpath: {e}")))?
                .to_string_lossy()
                .replace('\\', "/");
            out.push(rel);
        }
    }
    Ok(())
}

impl DoctrinePack {
    pub fn gate(&self, id: &str) -> Option<&Value> {
        self.gates.iter().find(|g| g.id == id).map(|g| &g.raw)
    }

    /// Fixture directive: fixtures.<product>.<key> as string.
    pub fn fixture(&self, product: &str, key: &str) -> Option<&str> {
        self.fixtures.get(product)?.get(key)?.as_str()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn make_pack(dir: &Path) {
        fs::create_dir_all(dir.join("schemas")).unwrap();
        fs::write(
            dir.join("doctrine.json"),
            r#"{"pack":"galahad","version":"0.1.0","fixtures":{"highball":{"decision":"DENIED"}}}"#,
        )
        .unwrap();
        fs::write(
            dir.join("gates.json"),
            r#"{"schema":"galahad.gates.v0","gates":[{"id":"g1","kind":"receipt_flag","flag":"verdict","op":"==","value":"PASS","on_fail":"blocked"}]}"#,
        )
        .unwrap();
        fs::write(
            dir.join("schemas").join("x.schema.json"),
            r#"{"type":"object"}"#,
        )
        .unwrap();
    }

    #[test]
    fn digest_covers_every_file() {
        let tmp = std::env::temp_dir().join(format!(
            "stammtisch-doctest-{}",
            crate::ids::uuid_v7().unwrap()
        ));
        let dir = tmp.join("galahad");
        make_pack(&dir);
        let pack = load_dir(&dir).unwrap();
        assert_eq!(pack.name, "galahad");
        assert!(pack.digest.starts_with("sha256:"));
        assert_eq!(pack.gates.len(), 1);
        assert_eq!(pack.fixture("highball", "decision"), Some("DENIED"));

        // One-byte doctrine change => digest drift.
        fs::write(
            dir.join("schemas").join("x.schema.json"),
            r#"{"type":"array"}"#,
        )
        .unwrap();
        let drifted = load_dir(&dir).unwrap();
        assert_ne!(pack.digest, drifted.digest);
        fs::remove_dir_all(&tmp).ok();
    }

    #[test]
    fn missing_pack_errors() {
        let tmp = std::env::temp_dir().join(format!(
            "stammtisch-doctest-{}",
            crate::ids::uuid_v7().unwrap()
        ));
        fs::create_dir_all(&tmp).unwrap();
        let e = load_dir(&tmp).unwrap_err();
        assert_eq!(e.code, "doctrine_unreadable");
        fs::remove_dir_all(&tmp).ok();
    }
}
