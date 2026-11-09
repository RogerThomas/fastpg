//! Calendar arithmetic for PostgreSQL's binary date/time formats.
//!
//! PostgreSQL counts days / microseconds from 2000-01-01 (00:00 UTC for timestamptz).

pub const USECS_PER_SEC: i64 = 1_000_000;
pub const USECS_PER_DAY: i64 = 86_400 * USECS_PER_SEC;
/// Days from 1970-01-01 to 2000-01-01.
pub const PG_EPOCH_UNIX_DAYS: i64 = 10_957;
/// Seconds from 1970-01-01 to 2000-01-01.
pub const PG_EPOCH_UNIX_SECS: i64 = PG_EPOCH_UNIX_DAYS * 86_400;

/// (year, month, day) for a count of days since 1970-01-01 (Howard Hinnant's algorithm).
pub fn civil_from_unix_days(z: i64) -> (i64, u8, u8) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u8;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u8;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// Days since 1970-01-01 for a proleptic Gregorian date.
pub fn unix_days_from_civil(y: i64, m: u8, d: u8) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y.rem_euclid(400);
    let m = i64::from(m);
    let mp = if m > 2 { m - 3 } else { m + 9 };
    let doy = (153 * mp + 2) / 5 + i64::from(d) - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// A date that fits Python's `datetime.date` (years 1..=9999).
#[derive(Clone, Copy)]
pub struct Ymd {
    pub year: i32,
    pub month: u8,
    pub day: u8,
}

pub fn ymd_from_pg_days(days: i32) -> Result<Ymd, String> {
    if days == i32::MAX || days == i32::MIN {
        return Err("infinite dates cannot be represented in Python".into());
    }
    let (y, month, day) = civil_from_unix_days(i64::from(days) + PG_EPOCH_UNIX_DAYS);
    if !(1..=9999).contains(&y) {
        return Err(format!("year {y} is out of range for Python dates"));
    }
    Ok(Ymd {
        year: y as i32,
        month,
        day,
    })
}

#[derive(Clone, Copy)]
pub struct Hmsu {
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
    pub micro: u32,
}

pub fn hmsu_from_usecs(usecs: i64) -> Hmsu {
    let secs = usecs / USECS_PER_SEC;
    Hmsu {
        hour: (secs / 3600) as u8,
        minute: ((secs / 60) % 60) as u8,
        second: (secs % 60) as u8,
        micro: (usecs % USECS_PER_SEC) as u32,
    }
}

/// Split a PostgreSQL timestamp (µs since 2000-01-01) into calendar date + time of day.
pub fn split_pg_timestamp(usecs: i64) -> Result<(Ymd, Hmsu), String> {
    if usecs == i64::MAX || usecs == i64::MIN {
        return Err("infinite timestamps cannot be represented in Python".into());
    }
    let days = usecs.div_euclid(USECS_PER_DAY);
    let tod = usecs.rem_euclid(USECS_PER_DAY);
    let days = i32::try_from(days).map_err(|_| "timestamp out of range".to_string())?;
    Ok((ymd_from_pg_days(days)?, hmsu_from_usecs(tod)))
}

/// Append `value`'s last `width` decimal digits, zero-padded (no `core::fmt`: this runs per
/// cell on the msgpack and JSON routes).
fn push_digits(out: &mut String, value: u32, width: usize) {
    let mut buf = [b'0'; 10];
    let mut v = value;
    for slot in buf[10 - width..].iter_mut().rev() {
        *slot = b'0' + (v % 10) as u8;
        v /= 10;
    }
    // ASCII digits only.
    out.extend(buf[10 - width..].iter().map(|&b| b as char));
}

/// ISO-8601 rendering (used by the msgpack path, which hands strings to msgspec).
pub fn fmt_date(out: &mut String, d: Ymd) {
    // `Ymd` years are 1..=9999.
    push_digits(out, d.year as u32, 4);
    out.push('-');
    push_digits(out, u32::from(d.month), 2);
    out.push('-');
    push_digits(out, u32::from(d.day), 2);
}

pub fn fmt_time(out: &mut String, t: Hmsu) {
    push_digits(out, u32::from(t.hour), 2);
    out.push(':');
    push_digits(out, u32::from(t.minute), 2);
    out.push(':');
    push_digits(out, u32::from(t.second), 2);
    if t.micro != 0 {
        out.push('.');
        push_digits(out, t.micro, 6);
    }
}

/// The canonical text of a UUID (`8-4-4-4-12` lowercase hex digits).
pub fn uuid_text(n: u128) -> [u8; 36] {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = [b'-'; 36];
    let mut nibble = 0;
    for (i, slot) in out.iter_mut().enumerate() {
        if matches!(i, 8 | 13 | 18 | 23) {
            continue;
        }
        *slot = HEX[((n >> (124 - 4 * nibble)) & 0xf) as usize];
        nibble += 1;
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn civil_roundtrip() {
        for days in [-1_000_000i64, -719_162, -1, 0, 1, 10_957, 2_932_896] {
            let (y, m, d) = civil_from_unix_days(days);
            assert_eq!(unix_days_from_civil(y, m, d), days, "{y}-{m}-{d}");
        }
        assert_eq!(civil_from_unix_days(0), (1970, 1, 1));
        assert_eq!(civil_from_unix_days(PG_EPOCH_UNIX_DAYS), (2000, 1, 1));
        assert_eq!(civil_from_unix_days(-719_162), (1, 1, 1));
    }

    // The `format!`-based renderings the functions above replaced.
    fn fmt_date_reference(d: Ymd) -> String {
        format!("{:04}-{:02}-{:02}", d.year, d.month, d.day)
    }

    fn fmt_time_reference(t: Hmsu) -> String {
        let mut s = format!("{:02}:{:02}:{:02}", t.hour, t.minute, t.second);
        if t.micro != 0 {
            s.push_str(&format!(".{:06}", t.micro));
        }
        s
    }

    fn uuid_text_reference(n: u128) -> String {
        let h = format!("{n:032x}");
        format!(
            "{}-{}-{}-{}-{}",
            &h[0..8],
            &h[8..12],
            &h[12..16],
            &h[16..20],
            &h[20..32]
        )
    }

    /// Days since 2000-01-01 of 0001-01-01 and 9999-12-31, the range of Python dates.
    const FIRST_DAY: i32 = (-719_162 - PG_EPOCH_UNIX_DAYS) as i32;
    const LAST_DAY: i32 = (2_932_896 - PG_EPOCH_UNIX_DAYS) as i32;

    #[test]
    fn dates_match_format() {
        let days = (FIRST_DAY..=LAST_DAY)
            .step_by(997)
            .chain([FIRST_DAY, -1, 0, 1, LAST_DAY]);
        for day in days {
            let d = ymd_from_pg_days(day).unwrap();
            let mut s = String::new();
            fmt_date(&mut s, d);
            assert_eq!(s, fmt_date_reference(d), "day {day}");
        }
        let mut s = String::new();
        fmt_date(&mut s, ymd_from_pg_days(FIRST_DAY).unwrap());
        assert_eq!(s, "0001-01-01");
        s.clear();
        fmt_date(&mut s, ymd_from_pg_days(LAST_DAY).unwrap());
        assert_eq!(s, "9999-12-31");
        s.clear();
        let d = Ymd {
            year: 999,
            month: 2,
            day: 3,
        };
        fmt_date(&mut s, d);
        assert_eq!(s, "0999-02-03");
        // BC dates, years past 9999 and ±infinity never reach the formatter.
        assert!(ymd_from_pg_days(FIRST_DAY - 1).is_err());
        assert!(ymd_from_pg_days(LAST_DAY + 1).is_err());
        assert!(ymd_from_pg_days(i32::MAX).is_err());
        assert!(ymd_from_pg_days(i32::MIN).is_err());
    }

    #[test]
    fn times_match_format() {
        let micros = [0, 1, 9, 10, 999, 1_000, 123_456, 500_000, 999_999];
        for secs in (0..86_400).step_by(7).chain([86_399]) {
            for micro in micros {
                let t = hmsu_from_usecs(secs * USECS_PER_SEC + micro);
                let mut s = String::new();
                fmt_time(&mut s, t);
                assert_eq!(s, fmt_time_reference(t), "{secs}s {micro}us");
            }
        }
        let mut s = String::new();
        fmt_time(&mut s, hmsu_from_usecs(3_723 * USECS_PER_SEC + 40));
        assert_eq!(s, "01:02:03.000040");
        s.clear();
        fmt_time(&mut s, hmsu_from_usecs(USECS_PER_DAY - 1));
        assert_eq!(s, "23:59:59.999999");
        s.clear();
        fmt_time(&mut s, hmsu_from_usecs(0));
        assert_eq!(s, "00:00:00");
    }

    #[test]
    fn timestamps_match_format() {
        let first = i64::from(FIRST_DAY) * USECS_PER_DAY;
        let last = (i64::from(LAST_DAY) + 1) * USECS_PER_DAY - 1;
        let samples = (first..=last)
            .step_by(98_765_432_109_877)
            .chain([first, -1, 0, 1, last]);
        for usecs in samples {
            let (d, t) = split_pg_timestamp(usecs).unwrap();
            let mut s = String::new();
            fmt_date(&mut s, d);
            s.push('T');
            fmt_time(&mut s, t);
            let expected = format!("{}T{}", fmt_date_reference(d), fmt_time_reference(t));
            assert_eq!(s, expected, "usecs {usecs}");
        }
        assert!(split_pg_timestamp(first - 1).is_err());
        assert!(split_pg_timestamp(last + 1).is_err());
        assert!(split_pg_timestamp(i64::MAX).is_err());
        assert!(split_pg_timestamp(i64::MIN).is_err());
    }

    #[test]
    fn uuids_match_format() {
        let mut n: u128 = 0x0123_4567_89ab_cdef_fedc_ba98_7654_3210;
        let mut values = vec![0, 1, u128::MAX, n, 0xf << 124];
        for _ in 0..1_000 {
            // xorshift
            n ^= n << 13;
            n ^= n >> 7;
            n ^= n << 17;
            values.push(n);
        }
        for v in values {
            let text = uuid_text(v);
            assert_eq!(std::str::from_utf8(&text).unwrap(), uuid_text_reference(v));
        }
        assert_eq!(
            &uuid_text(0x0123_4567_89ab_cdef_fedc_ba98_7654_3210),
            b"01234567-89ab-cdef-fedc-ba9876543210"
        );
    }

    #[test]
    fn negative_timestamps_floor() {
        let (d, t) = split_pg_timestamp(-1).unwrap();
        assert_eq!((d.year, d.month, d.day), (1999, 12, 31));
        assert_eq!((t.hour, t.minute, t.second, t.micro), (23, 59, 59, 999_999));
    }
}
