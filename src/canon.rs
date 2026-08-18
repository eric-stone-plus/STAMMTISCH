//! Canonical JSON and SHA-256 digests.
//!
//! Canonical form = compact `serde_json` serialization. The crate builds
//! `serde_json` without `preserve_order`, so objects are BTreeMaps and keys
//! serialize in sorted order — the form is therefore deterministic for any
//! semantically-equal document, which is exactly what content addressing
//! needs (architecture doc §3.1 canonicalization discipline).

use serde_json::Value;
use sha2::{Digest, Sha256};

pub fn canonical(v: &Value) -> String {
    // to_string on Value is deterministic given sorted BTreeMap keys.
    serde_json::to_string(v).expect("Value serialization is infallible")
}

pub fn canonical_bytes(v: &Value) -> Vec<u8> {
    canonical(v).into_bytes()
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    let out = h.finalize();
    out.iter().map(|b| format!("{b:02x}")).collect()
}

/// `sha256:<hex>` — the prefixed form every schema pattern requires.
pub fn sha256_prefixed(bytes: &[u8]) -> String {
    format!("sha256:{}", sha256_hex(bytes))
}

pub fn sha256_value_prefixed(v: &Value) -> String {
    sha256_prefixed(&canonical_bytes(v))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn canonical_sorts_keys() {
        let v = json!({"b": 1, "a": {"z": 2, "y": 3}});
        assert_eq!(canonical(&v), r#"{"a":{"y":3,"z":2},"b":1}"#);
    }

    #[test]
    fn digest_is_stable_and_prefixed() {
        let d = sha256_prefixed(b"stammtisch");
        assert!(d.starts_with("sha256:"));
        assert_eq!(d.len(), 7 + 64);
        assert_eq!(d, sha256_prefixed(b"stammtisch"));
        // Known vector: sha256("") = e3b0c442...
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn digest_detects_single_byte_flip() {
        let a = sha256_hex(b"evidence");
        let mut tampered = b"evidence".to_vec();
        tampered[3] ^= 0x01;
        assert_ne!(a, sha256_hex(&tampered));
    }
}
