//! Test-only scratch directories, compiled out of every non-test build.
//!
//! Before this module existed, inline tests created uuid-suffixed dirs under
//! `std::env::temp_dir()` with no cleanup; every `cargo test` leaked a fresh
//! batch (measured 2026-09-18: hundreds of `stammtisch-*` dirs under /tmp,
//! which is a tmpfs — the leak burns RAM). [`scratch`] returns a guard that
//! removes the directory on drop; cleanup is best-effort so it can never mask
//! a failing assertion.

#[cfg(test)]
pub(crate) struct TmpDir(std::path::PathBuf);

#[cfg(test)]
impl TmpDir {
    pub(crate) fn new(prefix: &str) -> Self {
        let path = std::env::temp_dir()
            .join(format!("{}-{}", prefix, crate::ids::uuid_v7().unwrap()));
        std::fs::create_dir_all(&path).unwrap();
        TmpDir(path)
    }
}

#[cfg(test)]
impl Drop for TmpDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

#[cfg(test)]
impl AsRef<std::path::Path> for TmpDir {
    fn as_ref(&self) -> &std::path::Path {
        &self.0
    }
}

#[cfg(test)]
impl std::ops::Deref for TmpDir {
    type Target = std::path::Path;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

/// Create a cleaned-up scratch dir named `{prefix}-{uuidv7}` under temp_dir().
#[cfg(test)]
pub(crate) fn scratch(prefix: &str) -> TmpDir {
    TmpDir::new(prefix)
}
