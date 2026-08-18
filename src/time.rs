//! UTC wall-clock timestamps without a date library (no chrono dependency).

use std::time::{SystemTime, UNIX_EPOCH};

/// Current time as RFC 3339 with millisecond precision, e.g.
/// `2026-08-09T00:50:53.457Z`. Suitable for the schemas' `date-time` fields.
pub fn now_rfc3339() -> String {
    let dur = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default();
    let secs = dur.as_secs() as i64;
    let millis = dur.subsec_millis();
    format_rfc3339(secs, millis)
}

pub fn format_rfc3339(secs_since_epoch: i64, millis: u32) -> String {
    let days = secs_since_epoch.div_euclid(86_400);
    let rem = secs_since_epoch.rem_euclid(86_400);
    let (h, mi, s) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    let (y, mo, d) = civil_from_days(days);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        y, mo, d, h, mi, s, millis
    )
}

/// Days since 1970-01-01 -> (year, month, day). Howard Hinnant's
/// civil-from-days algorithm, exact for the whole i64 range.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097); // day of era, [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn epoch_is_1970() {
        assert_eq!(format_rfc3339(0, 0), "1970-01-01T00:00:00.000Z");
    }

    #[test]
    fn known_instant() {
        // 2026-08-09T00:50:53.457Z
        let secs = 1_786_236_653;
        assert_eq!(format_rfc3339(secs, 457), "2026-08-09T00:50:53.457Z");
    }

    #[test]
    fn leap_day() {
        // 2024-02-29T12:00:00Z = 1709208000
        assert_eq!(format_rfc3339(1_709_208_000, 0), "2024-02-29T12:00:00.000Z");
    }

    #[test]
    fn now_parses_shape() {
        let s = now_rfc3339();
        assert_eq!(s.len(), 24);
        assert!(s.ends_with('Z'));
        assert_eq!(&s[4..5], "-");
        assert_eq!(&s[10..11], "T");
    }
}
