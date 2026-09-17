//! Shared test support for the integration test crates. Nothing here is
//! shipped; the support modules are compiled per test binary.

pub mod fake_a2a;

/// RAII scratch dir under temp_dir(), removed on drop (best-effort).
/// Same contract as src/testutil.rs for the inline tests; shared by the
/// integration crates to stop the per-run /tmp accumulation.
#[allow(dead_code)]  // per-test-binary compilation: not every crate uses it
pub struct TmpDir(std::path::PathBuf);

impl TmpDir {
    #[allow(dead_code)]
    pub fn new(prefix: &str) -> Self {
        let path = std::env::temp_dir()
            .join(format!("{}-{}", prefix, stammtisch::ids::uuid_v7().unwrap()));
        std::fs::create_dir_all(&path).unwrap();
        TmpDir(path)
    }
}

impl Drop for TmpDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

impl std::ops::Deref for TmpDir {
    type Target = std::path::Path;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl AsRef<std::path::Path> for TmpDir {
    fn as_ref(&self) -> &std::path::Path {
        &self.0
    }
}
