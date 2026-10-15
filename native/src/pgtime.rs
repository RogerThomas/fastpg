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

/// ISO-8601 rendering (used by the msgpack path, which hands strings to msgspec).
pub fn fmt_date(out: &mut String, d: Ymd) {
    use std::fmt::Write;
    let _ = write!(out, "{:04}-{:02}-{:02}", d.year, d.month, d.day);
}

pub fn fmt_time(out: &mut String, t: Hmsu) {
    use std::fmt::Write;
    let _ = write!(out, "{:02}:{:02}:{:02}", t.hour, t.minute, t.second);
    if t.micro != 0 {
        let _ = write!(out, ".{:06}", t.micro);
    }
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

    #[test]
    fn negative_timestamps_floor() {
        let (d, t) = split_pg_timestamp(-1).unwrap();
        assert_eq!((d.year, d.month, d.day), (1999, 12, 31));
        assert_eq!((t.hour, t.minute, t.second, t.micro), (23, 59, 59, 999_999));
    }
}
