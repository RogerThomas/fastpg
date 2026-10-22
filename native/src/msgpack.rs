//! Rows → MessagePack, for decoding with a cached `msgspec.msgpack.Decoder(T)`.
//!
//! This is the alternative strategy to direct construction: every row becomes a map of
//! `column name → value`, and msgspec performs field matching, validation (constraints,
//! enums, literals, nested types) and construction. Values are encoded in the forms
//! msgspec's msgpack decoder accepts for the corresponding Python types.

use rmp::encode as mp;

use crate::decode::{ArrayItems, Codec, char_text, numeric_to_string, read_i64};
use crate::pgtime::{self, PG_EPOCH_UNIX_SECS, USECS_PER_SEC};
use crate::rows::RowBuffer;

/// Encode `rows` as either a single map (`single = true`, exactly one row expected) or an
/// array of maps.
pub fn encode_rows(
    rows: &RowBuffer,
    names: &[&str],
    codecs: &[Codec],
    as_text: &[bool],
    single: bool,
) -> Result<Vec<u8>, String> {
    let mut out = Vec::with_capacity(64 + rows.nrows() * names.len() * 16);
    if !single {
        let _ = mp::write_array_len(&mut out, rows.nrows() as u32);
    }
    let mut scratch = String::new();
    for row in 0..rows.nrows() {
        let _ = mp::write_map_len(&mut out, names.len() as u32);
        for (col, (name, codec)) in names.iter().zip(codecs).enumerate() {
            let _ = mp::write_str(&mut out, name);
            match rows.cell(row, col) {
                None => {
                    let _ = mp::write_nil(&mut out);
                }
                Some(raw) => encode_value(&mut out, &mut scratch, *codec, as_text[col], raw)
                    .map_err(|e| format!("column {name:?}: {e}"))?,
            }
        }
    }
    Ok(out)
}

fn fixed<const N: usize>(raw: &[u8]) -> Result<[u8; N], String> {
    raw.try_into()
        .map_err(|_| format!("malformed binary value ({} bytes)", raw.len()))
}

/// `as_text`: a json/jsonb value is written as a string holding its JSON text (it feeds a
/// `str` field, ADR 0003) instead of as the MessagePack form of the JSON value.
fn encode_value(
    out: &mut Vec<u8>,
    scratch: &mut String,
    codec: Codec,
    as_text: bool,
    raw: &[u8],
) -> Result<(), String> {
    scratch.clear();
    match codec {
        Codec::Void => {
            let _ = mp::write_nil(out);
        }
        Codec::Bool => {
            let [b] = fixed::<1>(raw)?;
            let _ = mp::write_bool(out, b != 0);
        }
        Codec::Int2 | Codec::Int4 | Codec::Int8 | Codec::Oid => {
            let _ = mp::write_sint(out, read_i64(codec, raw)?);
        }
        Codec::Float4 => {
            let _ = mp::write_f64(out, f64::from(f32::from_be_bytes(fixed(raw)?)));
        }
        Codec::Float8 => {
            let _ = mp::write_f64(out, f64::from_be_bytes(fixed(raw)?));
        }
        Codec::Text => {
            let s = std::str::from_utf8(raw).map_err(|e| e.to_string())?;
            let _ = mp::write_str(out, s);
        }
        Codec::Char => {
            let [b] = fixed::<1>(raw)?;
            let _ = mp::write_str(out, char_text(b, &mut [0; 4]));
        }
        Codec::Bytea => {
            let _ = mp::write_bin(out, raw);
        }
        Codec::Uuid => {
            let n = u128::from_be_bytes(fixed(raw)?);
            let h = format!("{n:032x}");
            scratch.push_str(&format!(
                "{}-{}-{}-{}-{}",
                &h[0..8],
                &h[8..12],
                &h[12..16],
                &h[16..20],
                &h[20..32]
            ));
            let _ = mp::write_str(out, scratch);
        }
        Codec::Date => {
            pgtime::fmt_date(
                scratch,
                pgtime::ymd_from_pg_days(i32::from_be_bytes(fixed(raw)?))?,
            );
            let _ = mp::write_str(out, scratch);
        }
        Codec::Time => {
            pgtime::fmt_time(
                scratch,
                pgtime::hmsu_from_usecs(i64::from_be_bytes(fixed(raw)?)),
            );
            let _ = mp::write_str(out, scratch);
        }
        Codec::Timestamp => {
            let (d, t) = pgtime::split_pg_timestamp(i64::from_be_bytes(fixed(raw)?))?;
            pgtime::fmt_date(scratch, d);
            scratch.push('T');
            pgtime::fmt_time(scratch, t);
            let _ = mp::write_str(out, scratch);
        }
        Codec::TimestampTz => {
            // msgpack timestamp extension (type -1), 96-bit form: u32 nanos + i64 seconds.
            let usecs = i64::from_be_bytes(fixed(raw)?);
            pgtime::split_pg_timestamp(usecs)?; // range check (rejects ±infinity)
            let secs = usecs.div_euclid(USECS_PER_SEC) + PG_EPOCH_UNIX_SECS;
            let nanos = (usecs.rem_euclid(USECS_PER_SEC) * 1000) as u32;
            let _ = mp::write_ext_meta(out, 12, -1);
            out.extend_from_slice(&nanos.to_be_bytes());
            out.extend_from_slice(&secs.to_be_bytes());
        }
        Codec::Numeric => {
            let _ = mp::write_str(out, &numeric_to_string(raw)?);
        }
        Codec::Json | Codec::Jsonb => {
            let text = match (codec, raw.split_first()) {
                (Codec::Jsonb, Some((1, rest))) => rest,
                (Codec::Jsonb, _) => return Err("malformed jsonb value".into()),
                _ => raw,
            };
            if as_text {
                let s = std::str::from_utf8(text).map_err(|e| e.to_string())?;
                let _ = mp::write_str(out, s);
            } else {
                let value: serde_json::Value =
                    serde_json::from_slice(text).map_err(|e| e.to_string())?;
                write_json(out, &value);
            }
        }
        Codec::Array(elem) => {
            let items = ArrayItems::parse(raw)?;
            let _ = mp::write_array_len(out, items.len() as u32);
            for item in items {
                match item? {
                    None => {
                        let _ = mp::write_nil(out);
                    }
                    Some(value) => encode_value(out, scratch, elem.codec(), as_text, value)?,
                }
            }
        }
    }
    Ok(())
}

fn write_json(out: &mut Vec<u8>, v: &serde_json::Value) {
    use serde_json::Value;
    match v {
        Value::Null => {
            let _ = mp::write_nil(out);
        }
        Value::Bool(b) => {
            let _ = mp::write_bool(out, *b);
        }
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                let _ = mp::write_sint(out, i);
            } else if let Some(u) = n.as_u64() {
                let _ = mp::write_uint(out, u);
            } else {
                let _ = mp::write_f64(out, n.as_f64().unwrap_or(f64::NAN));
            }
        }
        Value::String(s) => {
            let _ = mp::write_str(out, s);
        }
        Value::Array(items) => {
            let _ = mp::write_array_len(out, items.len() as u32);
            for item in items {
                write_json(out, item);
            }
        }
        Value::Object(map) => {
            let _ = mp::write_map_len(out, map.len() as u32);
            for (k, item) in map {
                let _ = mp::write_str(out, k);
                write_json(out, item);
            }
        }
    }
}
