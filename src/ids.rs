//! Hand-rolled UUIDv7 run identifiers (RFC 9562 §5.7): 48-bit unix
//! milliseconds, version 7, variant 10, random tail.

use std::time::{SystemTime, UNIX_EPOCH};

use crate::error::AppError;
use crate::random;

pub fn uuid_v7() -> Result<String, AppError> {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64; // 48 bits suffice until year 10889
    let mut rnd = [0u8; 10];
    random::fill(&mut rnd)?;

    let mut b = [0u8; 16];
    b[0] = (millis >> 40) as u8;
    b[1] = (millis >> 32) as u8;
    b[2] = (millis >> 24) as u8;
    b[3] = (millis >> 16) as u8;
    b[4] = (millis >> 8) as u8;
    b[5] = millis as u8;
    b[6] = 0x70 | (rnd[0] & 0x0f); // version 7
    b[7] = rnd[1];
    b[8] = 0x80 | (rnd[2] & 0x3f); // variant 10
    b[9..].copy_from_slice(&rnd[3..]);

    let h: String = b.iter().map(|x| format!("{x:02x}")).collect();
    Ok(format!(
        "{}-{}-{}-{}-{}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shape_matches_manifest_pattern() {
        let id = uuid_v7().unwrap();
        assert_eq!(id.len(), 36);
        let pat = "^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$";
        assert!(crate::pattern::matches(pat, &id).unwrap());
    }

    #[test]
    fn time_ordered() {
        let a = uuid_v7().unwrap();
        std::thread::sleep(std::time::Duration::from_millis(2));
        let b = uuid_v7().unwrap();
        assert!(a < b); // ms prefix dominates string order
    }

    #[test]
    fn distinct() {
        assert_ne!(uuid_v7().unwrap(), uuid_v7().unwrap());
    }
}
