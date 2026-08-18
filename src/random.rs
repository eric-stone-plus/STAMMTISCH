//! OS randomness via /dev/urandom — no rand crate.

use std::fs::File;
use std::io::Read;

use crate::error::AppError;

/// Fill `buf` with cryptographically-adequate random bytes from the OS.
pub fn fill(buf: &mut [u8]) -> Result<(), AppError> {
    let mut f = File::open("/dev/urandom")
        .map_err(|e| AppError::internal(format!("open /dev/urandom: {e}")))?;
    f.read_exact(buf)
        .map_err(|e| AppError::internal(format!("read /dev/urandom: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fills_with_nonzero_data() {
        let mut a = [0u8; 32];
        let mut b = [0u8; 32];
        fill(&mut a).unwrap();
        fill(&mut b).unwrap();
        assert_ne!(a, [0u8; 32]);
        assert_ne!(a, b); // astronomically unlikely to collide
    }
}
