//! Rows → JSON, for validating parsers that run in native code:
//! pydantic-core's `validate_json` (jiter) and `msgspec.json.Decoder`.
//!
//! Each row becomes an object of `column name → value`. Values use the JSON forms both
//! libraries accept for the corresponding Python types: numerics and UUIDs as strings,
//! dates/times as ISO-8601, timestamptz with a `Z` suffix, bytea as base64 (msgspec's JSON
//! convention; pydantic needs `val_json_bytes='base64'`), json/jsonb embedded verbatim
//! (or as a string of their text, for `str` fields).
//! Non-finite floats use the `NaN`/`Infinity` tokens pydantic accepts. msgspec rejects them,
//! and has no other JSON form for them: for msgspec (`non_finite = false`) they are an error.

use crate::decode::{Codec, char_text, read_i64, write_numeric};
use crate::pgtime;
use crate::rows::RowBuffer;

/// Encode `rows` as a single object (`single`, one row expected) or an array of objects.
/// `non_finite`: the decoder accepts `NaN` / `Infinity` / `-Infinity` tokens (pydantic).
pub fn encode_rows(
    rows: &RowBuffer,
    names: &[&str],
    codecs: &[Codec],
    as_text: &[bool],
    single: bool,
    non_finite: bool,
) -> Result<Vec<u8>, String> {
    let mut out = Vec::with_capacity(64 + rows.nrows() * names.len() * 24);
    // Column names are escaped once, as `"name":` prefixes.
    let keys: Vec<Vec<u8>> = names
        .iter()
        .map(|n| {
            let mut k = Vec::with_capacity(n.len() + 3);
            write_str(&mut k, n);
            k.push(b':');
            k
        })
        .collect();
    let mut scratch = String::new();
    if !single {
        out.push(b'[');
    }
    for row in 0..rows.nrows() {
        if row > 0 {
            out.push(b',');
        }
        out.push(b'{');
        for (col, (key, codec)) in keys.iter().zip(codecs).enumerate() {
            if col > 0 {
                out.push(b',');
            }
            out.extend_from_slice(key);
            match rows.cell(row, col) {
                None => out.extend_from_slice(b"null"),
                Some(raw) => {
                    encode_value(
                        &mut out,
                        &mut scratch,
                        *codec,
                        as_text[col],
                        non_finite,
                        raw,
                    )
                    .map_err(|e| format!("column {:?}: {e}", names[col]))?;
                }
            }
        }
        out.push(b'}');
    }
    if !single {
        out.push(b']');
    }
    Ok(out)
}

fn fixed<const N: usize>(raw: &[u8]) -> Result<[u8; N], String> {
    raw.try_into()
        .map_err(|_| format!("malformed binary value ({} bytes)", raw.len()))
}

/// `codec` names the column type in the error for a non-finite value msgspec can't take.
fn write_f64(out: &mut Vec<u8>, v: f64, non_finite: bool, codec: &str) -> Result<(), String> {
    if v.is_finite() {
        out.extend_from_slice(ryu::Buffer::new().format_finite(v).as_bytes());
        return Ok(());
    }
    let token = if v.is_nan() {
        "NaN"
    } else if v > 0.0 {
        "Infinity"
    } else {
        "-Infinity"
    };
    if !non_finite {
        return Err(format!(
            "{codec} {token} has no JSON form msgspec accepts; use strategy=\"direct\" or \
             \"msgpack\""
        ));
    }
    out.extend_from_slice(token.as_bytes());
    Ok(())
}

/// Write `s` as a JSON string literal; unescaped runs are copied in bulk.
fn write_str(out: &mut Vec<u8>, s: &str) {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    out.push(b'"');
    let bytes = s.as_bytes();
    let mut start = 0;
    for (i, &b) in bytes.iter().enumerate() {
        let escape: &[u8] = match b {
            b'"' => b"\\\"",
            b'\\' => b"\\\\",
            b'\n' => b"\\n",
            b'\r' => b"\\r",
            b'\t' => b"\\t",
            0..=0x1f => b"",
            _ => continue,
        };
        out.extend_from_slice(&bytes[start..i]);
        if escape.is_empty() {
            out.extend_from_slice(&[
                b'\\',
                b'u',
                b'0',
                b'0',
                HEX[(b >> 4) as usize],
                HEX[(b & 0xf) as usize],
            ]);
        } else {
            out.extend_from_slice(escape);
        }
        start = i + 1;
    }
    out.extend_from_slice(&bytes[start..]);
    out.push(b'"');
}

fn write_base64(out: &mut Vec<u8>, data: &[u8]) {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    out.push(b'"');
    for chunk in data.chunks(3) {
        let n = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
        out.push(ALPHABET[(n >> 18) as usize & 63]);
        out.push(ALPHABET[(n >> 12) as usize & 63]);
        out.push(if chunk.len() > 1 {
            ALPHABET[(n >> 6) as usize & 63]
        } else {
            b'='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[n as usize & 63]
        } else {
            b'='
        });
    }
    out.push(b'"');
}

/// `as_text`: a json/jsonb value is written as a string literal holding its JSON text (it
/// feeds a `str` field, ADR 0003) instead of embedded verbatim. `non_finite`: see
/// [`encode_rows`].
fn encode_value(
    out: &mut Vec<u8>,
    scratch: &mut String,
    codec: Codec,
    as_text: bool,
    non_finite: bool,
    raw: &[u8],
) -> Result<(), String> {
    scratch.clear();
    match codec {
        Codec::Void => out.extend_from_slice(b"null"),
        Codec::Bool => {
            let [b] = fixed::<1>(raw)?;
            out.extend_from_slice(if b != 0 { b"true" } else { b"false" });
        }
        Codec::Int2 | Codec::Int4 | Codec::Int8 | Codec::Oid => {
            out.extend_from_slice(itoa::Buffer::new().format(read_i64(codec, raw)?).as_bytes());
        }
        Codec::Float4 => {
            let v = f64::from(f32::from_be_bytes(fixed(raw)?));
            write_f64(out, v, non_finite, "float4")?;
        }
        Codec::Float8 => write_f64(out, f64::from_be_bytes(fixed(raw)?), non_finite, "float8")?,
        Codec::Text => write_str(out, std::str::from_utf8(raw).map_err(|e| e.to_string())?),
        Codec::Char => {
            let [b] = fixed::<1>(raw)?;
            write_str(out, char_text(b, &mut [0; 4]));
        }
        Codec::Bytea => write_base64(out, raw),
        Codec::Uuid => {
            let n = u128::from_be_bytes(fixed(raw)?);
            let h = format!("{n:032x}");
            out.push(b'"');
            for (i, part) in [&h[0..8], &h[8..12], &h[12..16], &h[16..20], &h[20..32]]
                .iter()
                .enumerate()
            {
                if i > 0 {
                    out.push(b'-');
                }
                out.extend_from_slice(part.as_bytes());
            }
            out.push(b'"');
        }
        Codec::Date => {
            pgtime::fmt_date(
                scratch,
                pgtime::ymd_from_pg_days(i32::from_be_bytes(fixed(raw)?))?,
            );
            write_str(out, scratch);
        }
        Codec::Time => {
            pgtime::fmt_time(
                scratch,
                pgtime::hmsu_from_usecs(i64::from_be_bytes(fixed(raw)?)),
            );
            write_str(out, scratch);
        }
        Codec::Timestamp | Codec::TimestampTz => {
            let (d, t) = pgtime::split_pg_timestamp(i64::from_be_bytes(fixed(raw)?))?;
            pgtime::fmt_date(scratch, d);
            scratch.push('T');
            pgtime::fmt_time(scratch, t);
            if codec == Codec::TimestampTz {
                scratch.push('Z');
            }
            write_str(out, scratch);
        }
        Codec::Numeric => {
            write_numeric(raw, scratch)?;
            write_str(out, scratch);
        }
        Codec::Json | Codec::Jsonb => {
            let text = match (codec, raw.split_first()) {
                (Codec::Jsonb, Some((1, rest))) => rest,
                (Codec::Jsonb, _) => return Err("malformed jsonb value".into()),
                _ => raw,
            };
            if as_text {
                write_str(out, std::str::from_utf8(text).map_err(|e| e.to_string())?);
            } else {
                out.extend_from_slice(text);
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn string_escaping() {
        let mut out = Vec::new();
        write_str(&mut out, "a\"b\\c\nd\u{1}é");
        assert_eq!(String::from_utf8(out).unwrap(), r#""a\"b\\c\nd\u0001é""#);
    }

    #[test]
    fn base64_padding() {
        for (input, expected) in [
            (&b""[..], "\"\""),
            (b"f", "\"Zg==\""),
            (b"fo", "\"Zm8=\""),
            (b"foo", "\"Zm9v\""),
        ] {
            let mut out = Vec::new();
            write_base64(&mut out, input);
            assert_eq!(String::from_utf8(out).unwrap(), expected);
        }
    }
}
