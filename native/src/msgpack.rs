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
                write_json(out, scratch, text)?;
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

/// MessagePack extension type of a JSON integer that fits neither `i64` nor `u64`, the
/// widest MessagePack integers. Its data is the integer's decimal text; the decoder's
/// `ext_hook` turns it back into an `int` (`fastpg._spec`, which uses the same code).
const BIG_INT_EXT: i8 = 1;

/// Write JSON `text` as the MessagePack form of its value, in one pass and without building
/// a tree, so that msgspec decodes the same value from it as `msgspec.json` does from the
/// text (the direct and json routes): object keys keep their order (and repeats, which
/// msgspec resolves the same way in both formats), integers keep every digit, and numbers
/// outside the float range are an error. Open containers are kept on a heap stack, so
/// nesting is limited only by msgspec's decoder, as on the other routes.
fn write_json(out: &mut Vec<u8>, scratch: &mut String, text: &[u8]) -> Result<(), String> {
    let mut p = JsonText { s: text, i: 0 };
    // Open containers: (offset of the 32-bit length to fill in, items so far, is object).
    let mut open: Vec<(usize, u32, bool)> = Vec::new();
    'value: loop {
        p.skip_ws();
        match p.peek() {
            Some(c @ (b'{' | b'[')) => {
                p.i += 1;
                let object = c == b'{';
                // map 32 / array 32: the length is known only at the end.
                out.push(if object { 0xdf } else { 0xdd });
                open.push((out.len(), 0, object));
                out.extend_from_slice(&[0; 4]);
                p.skip_ws();
                if p.eat(if object { b'}' } else { b']' }) {
                    open.pop(); // empty: the length stays 0
                } else {
                    if object {
                        p.key(out, scratch)?;
                    }
                    continue 'value;
                }
            }
            Some(b'"') => {
                p.i += 1;
                p.string(out, scratch)?;
            }
            Some(b't') => {
                p.literal(b"true")?;
                let _ = mp::write_bool(out, true);
            }
            Some(b'f') => {
                p.literal(b"false")?;
                let _ = mp::write_bool(out, false);
            }
            Some(b'n') => {
                p.literal(b"null")?;
                let _ = mp::write_nil(out);
            }
            _ => p.number(out)?,
        }
        // A value is complete: go on in its container, closing every container that ends.
        loop {
            let Some((at, len, object)) = open.last_mut() else {
                p.skip_ws();
                return match p.peek() {
                    None => Ok(()),
                    Some(_) => Err(p.error()),
                };
            };
            *len += 1;
            p.skip_ws();
            match p.next() {
                Some(b',') => {
                    if *object {
                        p.key(out, scratch)?;
                    }
                    continue 'value;
                }
                Some(b'}') if *object => {}
                Some(b']') if !*object => {}
                _ => return Err(p.error()),
            }
            let (at, len) = (*at, *len);
            out[at..at + 4].copy_from_slice(&len.to_be_bytes());
            open.pop();
        }
    }
}

/// A cursor over JSON text.
struct JsonText<'a> {
    s: &'a [u8],
    i: usize,
}

impl JsonText<'_> {
    fn error(&self) -> String {
        format!("invalid JSON at byte {}", self.i)
    }

    fn peek(&self) -> Option<u8> {
        self.s.get(self.i).copied()
    }

    fn next(&mut self) -> Option<u8> {
        let c = self.peek()?;
        self.i += 1;
        Some(c)
    }

    fn eat(&mut self, c: u8) -> bool {
        let found = self.peek() == Some(c);
        self.i += usize::from(found);
        found
    }

    fn skip_ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\n' | b'\r')) {
            self.i += 1;
        }
    }

    fn literal(&mut self, word: &[u8]) -> Result<(), String> {
        if !self.s[self.i..].starts_with(word) {
            return Err(self.error());
        }
        self.i += word.len();
        Ok(())
    }

    fn digits(&mut self) -> Result<(), String> {
        let start = self.i;
        while matches!(self.peek(), Some(b'0'..=b'9')) {
            self.i += 1;
        }
        if self.i == start {
            return Err(self.error());
        }
        Ok(())
    }

    /// An object member's key and the `:` after it.
    fn key(&mut self, out: &mut Vec<u8>, scratch: &mut String) -> Result<(), String> {
        self.skip_ws();
        if !self.eat(b'"') {
            return Err(self.error());
        }
        self.string(out, scratch)?;
        self.skip_ws();
        if !self.eat(b':') {
            return Err(self.error());
        }
        Ok(())
    }

    /// The rest of a string whose opening quote was consumed.
    fn string(&mut self, out: &mut Vec<u8>, scratch: &mut String) -> Result<(), String> {
        fn utf8(b: &[u8]) -> Result<&str, String> {
            std::str::from_utf8(b).map_err(|e| e.to_string())
        }
        let mut start = self.i;
        let mut escaped = false; // the text before `start` is in `scratch`
        loop {
            match self.peek() {
                Some(b'"') => {
                    let tail = utf8(&self.s[start..self.i])?;
                    self.i += 1;
                    if escaped {
                        scratch.push_str(tail);
                        let _ = mp::write_str(out, scratch);
                    } else {
                        let _ = mp::write_str(out, tail);
                    }
                    return Ok(());
                }
                Some(b'\\') => {
                    if !escaped {
                        scratch.clear();
                        escaped = true;
                    }
                    scratch.push_str(utf8(&self.s[start..self.i])?);
                    self.i += 1;
                    let c = match self.next() {
                        Some(b'"') => '"',
                        Some(b'\\') => '\\',
                        Some(b'/') => '/',
                        Some(b'b') => '\u{8}',
                        Some(b'f') => '\u{c}',
                        Some(b'n') => '\n',
                        Some(b'r') => '\r',
                        Some(b't') => '\t',
                        Some(b'u') => self.unicode_escape()?,
                        _ => return Err(self.error()),
                    };
                    scratch.push(c);
                    start = self.i;
                }
                Some(0x20..) => self.i += 1,
                _ => return Err(self.error()), // end of text, or a control character
            }
        }
    }

    /// The character of a `\u` escape whose `\u` was consumed, joining a surrogate pair.
    fn unicode_escape(&mut self) -> Result<char, String> {
        let high = self.hex4()?;
        let code = match high {
            0xd800..=0xdbff => {
                if !(self.eat(b'\\') && self.eat(b'u')) {
                    return Err(self.error());
                }
                let low = self.hex4()?;
                if !(0xdc00..=0xdfff).contains(&low) {
                    return Err(self.error());
                }
                0x10000 + ((high - 0xd800) << 10) + (low - 0xdc00)
            }
            0xdc00..=0xdfff => return Err(self.error()),
            _ => high,
        };
        char::from_u32(code).ok_or_else(|| self.error())
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let hex = self.s.get(self.i..self.i + 4).ok_or_else(|| self.error())?;
        if !hex.iter().all(u8::is_ascii_hexdigit) {
            return Err(self.error());
        }
        let code = hex
            .iter()
            .fold(0, |n, &b| n * 16 + char::from(b).to_digit(16).unwrap_or(0));
        self.i += 4;
        Ok(code)
    }

    /// A number, as `msgspec.json` reads it: without a fraction or exponent an integer of
    /// any size, otherwise the nearest float, which must be finite.
    fn number(&mut self, out: &mut Vec<u8>) -> Result<(), String> {
        let start = self.i;
        self.eat(b'-');
        if !self.eat(b'0') {
            self.digits()?;
        }
        let mut float = false;
        if self.eat(b'.') {
            float = true;
            self.digits()?;
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            float = true;
            self.i += 1;
            let _ = self.eat(b'+') || self.eat(b'-');
            self.digits()?;
        }
        // ASCII: only a sign, digits, '.' and an exponent were consumed.
        let text = std::str::from_utf8(&self.s[start..self.i]).map_err(|_| self.error())?;
        if float {
            let v: f64 = text.parse().map_err(|_| self.error())?;
            if !v.is_finite() {
                return Err(format!("number out of range: {text}"));
            }
            let _ = mp::write_f64(out, v);
        } else if let Ok(v) = text.parse::<i64>() {
            let _ = mp::write_sint(out, v);
        } else if let Ok(v) = text.parse::<u64>() {
            let _ = mp::write_uint(out, v);
        } else {
            let _ = mp::write_ext_meta(out, text.len() as u32, BIG_INT_EXT);
            out.extend_from_slice(text.as_bytes());
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn transcode(json: &str) -> Result<Vec<u8>, String> {
        let mut out = Vec::new();
        write_json(&mut out, &mut String::new(), json.as_bytes())?;
        Ok(out)
    }

    fn header(marker: u8, len: u32) -> Vec<u8> {
        let mut v = vec![marker];
        v.extend_from_slice(&len.to_be_bytes());
        v
    }

    fn map32(len: u32) -> Vec<u8> {
        header(0xdf, len)
    }

    fn array32(len: u32) -> Vec<u8> {
        header(0xdd, len)
    }

    fn string(s: &str) -> Vec<u8> {
        let mut v = Vec::new();
        mp::write_str(&mut v, s).unwrap();
        v
    }

    #[test]
    fn object_keys_keep_their_order_and_repeats() {
        let got = transcode(r#" { "z" : 1, "a" : 2, "z": 3 } "#).unwrap();
        let mut expected = map32(3);
        for (k, v) in [("z", 1), ("a", 2), ("z", 3)] {
            expected.extend(string(k));
            expected.push(v);
        }
        assert_eq!(got, expected);
    }

    #[test]
    fn nested_and_empty_containers() {
        let got = transcode(r#"[[], {}, [null, true, false], {"k": []}]"#).unwrap();
        let mut expected = array32(4);
        expected.extend(array32(0));
        expected.extend(map32(0));
        expected.extend(array32(3));
        expected.extend([0xc0, 0xc3, 0xc2]);
        expected.extend(map32(1));
        expected.extend(string("k"));
        expected.extend(array32(0));
        assert_eq!(got, expected);
    }

    #[test]
    fn deep_nesting_does_not_recurse() {
        let depth = 1_000_000;
        let json = "[".repeat(depth) + &"]".repeat(depth);
        let got = transcode(&json).unwrap();
        assert_eq!(got.len(), depth * 5);
        assert_eq!(got[..5], array32(1));
        assert_eq!(got[got.len() - 5..], array32(0));
    }

    #[test]
    fn integers_keep_every_digit() {
        let int = |v: i64| {
            let mut b = Vec::new();
            mp::write_sint(&mut b, v).unwrap();
            b
        };
        assert_eq!(transcode("-0").unwrap(), int(0));
        assert_eq!(transcode("-9223372036854775808").unwrap(), int(i64::MIN));
        let mut max = Vec::new();
        mp::write_uint(&mut max, u64::MAX).unwrap();
        assert_eq!(transcode("18446744073709551615").unwrap(), max);
        for big in [
            "18446744073709551616",
            "-9223372036854775809",
            "98765432109876543210",
        ] {
            let mut expected = Vec::new();
            mp::write_ext_meta(&mut expected, big.len() as u32, BIG_INT_EXT).unwrap();
            expected.extend_from_slice(big.as_bytes());
            assert_eq!(transcode(big).unwrap(), expected, "{big}");
        }
    }

    #[test]
    fn floats_round_to_nearest_and_must_be_finite() {
        let float = |v: f64| {
            let mut b = Vec::new();
            mp::write_f64(&mut b, v).unwrap();
            b
        };
        assert_eq!(transcode("1E2").unwrap(), float(100.0));
        assert_eq!(transcode("-0.0").unwrap(), float(-0.0));
        assert_eq!(transcode("1e-400").unwrap(), float(0.0));
        assert_eq!(
            transcode("0.1000000000000000055511151231257827").unwrap(),
            float(0.1)
        );
        for out_of_range in ["1e400", "-1e400"] {
            assert!(
                transcode(out_of_range)
                    .unwrap_err()
                    .contains("out of range")
            );
        }
    }

    #[test]
    fn string_escapes() {
        let got = transcode(r#""a\"\\\/\b\f\n\r\t\u00e9\ud83d\ude00\u0000""#).unwrap();
        assert_eq!(got, string("a\"\\/\u{8}\u{c}\n\r\t\u{e9}\u{1f600}\0"));
        assert_eq!(transcode("\"é\"").unwrap(), string("é"));
    }

    #[test]
    fn invalid_text_is_an_error() {
        for bad in [
            "",
            "[",
            "[1,]",
            "{\"a\" 1}",
            "{1: 2}",
            "01",
            "-",
            "1.",
            "1e",
            "tru",
            "[1] 2",
            "\"\\ud800\"",
            "\"\\udc00\"",
            "\"\\x\"",
            "\"a",
            "\"\u{1}\"",
        ] {
            assert!(transcode(bad).is_err(), "{bad:?}");
        }
        assert!(write_json(&mut Vec::new(), &mut String::new(), b"\"\xff\"").is_err());
    }
}
