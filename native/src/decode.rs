//! PostgreSQL binary format → Python objects.
//!
//! A [`Codec`] is chosen once per result column (per prepared statement) from the column's
//! PostgreSQL type; decoding a cell is then a branch on a small enum plus the object
//! constructor, with no intermediate Rust allocations for the common scalar types.

use std::ffi::c_char;

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBytes, PyDate, PyDateTime, PyFloat, PyString, PyTime, PyTuple, PyTzInfo};
use pyo3::{ffi, intern};
use xitca_postgres::types::{Kind, Type};

use crate::pgtime;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Codec {
    Bool,
    Int2,
    Int4,
    Int8,
    Oid,
    Float4,
    Float8,
    /// text, varchar, bpchar, name, citext, enums, unknown literals.
    Text,
    /// PostgreSQL's internal single-byte `"char"` (not `char(n)`, which is bpchar).
    Char,
    Bytea,
    Uuid,
    Date,
    Time,
    Timestamp,
    TimestampTz,
    Numeric,
    Json,
    Jsonb,
    /// `void` (e.g. `SELECT pg_sleep(1)`): always None.
    Void,
}

impl Codec {
    pub fn for_type(ty: &Type) -> Option<Self> {
        Some(match *ty {
            Type::BOOL => Self::Bool,
            Type::INT2 => Self::Int2,
            Type::INT4 => Self::Int4,
            Type::INT8 => Self::Int8,
            Type::OID => Self::Oid,
            Type::FLOAT4 => Self::Float4,
            Type::FLOAT8 => Self::Float8,
            Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME | Type::UNKNOWN => Self::Text,
            Type::CHAR => Self::Char,
            Type::BYTEA => Self::Bytea,
            Type::UUID => Self::Uuid,
            Type::DATE => Self::Date,
            Type::TIME => Self::Time,
            Type::TIMESTAMP => Self::Timestamp,
            Type::TIMESTAMPTZ => Self::TimestampTz,
            Type::NUMERIC => Self::Numeric,
            Type::JSON => Self::Json,
            Type::JSONB => Self::Jsonb,
            Type::VOID => Self::Void,
            _ => match ty.kind() {
                // Enum labels and citext travel as plain UTF-8 text in binary format.
                Kind::Enum(_) => Self::Text,
                Kind::Simple if ty.name() == "citext" => Self::Text,
                _ => return None,
            },
        })
    }

    pub fn is_int(self) -> bool {
        matches!(self, Self::Int2 | Self::Int4 | Self::Int8 | Self::Oid)
    }
}

/// Slot layout of `uuid.UUID` (`__slots__ = ('int', 'is_safe', '__weakref__')`).
///
/// `UUID.__init__` is pure Python (~280ns). The uuid module itself creates instances by
/// assigning the two slots directly; we do the same from Rust: allocate via `tp_alloc` and
/// store into the slot offsets published by the class's member descriptors (~20ns).
struct UuidLayout {
    int_offset: ffi::Py_ssize_t,
    is_safe_offset: ffi::Py_ssize_t,
    unknown: Py<PyAny>,
}

/// Byte offset of the `__slots__` entry `name` on `cls` (looked up through the MRO), if it is
/// a plain object slot (`Py_T_OBJECT_EX` member descriptor). `None` for anything else, so
/// callers can fall back to the generic attribute protocol.
pub fn member_offset(cls: &Bound<'_, PyAny>, name: &str) -> PyResult<Option<ffi::Py_ssize_t>> {
    let Ok(descr) = cls.getattr(name) else {
        return Ok(None);
    };
    // SAFETY: only reads the type pointer and, for genuine member descriptors, the
    // PyMemberDef they point to.
    unsafe {
        if ffi::Py_TYPE(descr.as_ptr()) != &raw mut ffi::PyMemberDescr_Type {
            return Ok(None);
        }
        let member = (*descr.as_ptr().cast::<ffi::PyMemberDescrObject>()).d_member;
        Ok(((*member).type_code == ffi::Py_T_OBJECT_EX).then_some((*member).offset))
    }
}

/// Take a freshly built object off the cyclic garbage collector's list. Only for objects
/// that can't be part of a reference cycle; CPython leaves tuples of atoms untracked the
/// same way.
///
/// `uuid.UUID` and `decimal.Decimal` instances are GC-tracked only because their types
/// support GC: a UUID references an `int` and `SafeUUID.unknown`, a Decimal nothing at all,
/// and neither can be mutated. Left tracked, every large result made the collector
/// traverse each of them while the rows were still alive (about 30 ns per value;
/// `notes/perf-decode.md`).
pub fn gc_untrack(obj: &Bound<'_, PyAny>) {
    // SAFETY: `obj` is live. PyObject_GC_UnTrack reads the GC header, so it is only called
    // for objects that have one, and it does nothing for an object that isn't tracked.
    unsafe {
        if ffi::PyObject_IS_GC(obj.as_ptr()) != 0 {
            ffi::PyObject_GC_UnTrack(obj.as_ptr().cast());
        }
    }
}

/// Store an owned reference into an empty (NULL) object slot.
///
/// # Safety
/// `offset` must come from [`member_offset`] for `obj`'s type, the slot must be NULL (fresh
/// from `tp_alloc`) and `value` must be an owned reference.
pub unsafe fn init_slot(
    obj: *mut ffi::PyObject,
    offset: ffi::Py_ssize_t,
    value: *mut ffi::PyObject,
) {
    // SAFETY: per this function's contract, `offset` is an object slot inside `obj`'s layout
    // (so the write is in bounds and aligned) and the slot is NULL (so no reference leaks).
    unsafe { *obj.cast::<u8>().offset(offset).cast::<*mut ffi::PyObject>() = value };
}

impl UuidLayout {
    fn resolve(cls: &Bound<'_, PyAny>) -> PyResult<Option<Self>> {
        let py = cls.py();
        let offset = |name: &str| member_offset(cls, name);
        let (Some(int_offset), Some(is_safe_offset)) = (offset("int")?, offset("is_safe")?) else {
            return Ok(None);
        };
        let unknown = py
            .import("uuid")?
            .getattr("SafeUUID")?
            .getattr("unknown")?
            .unbind();
        Ok(Some(Self {
            int_offset,
            is_safe_offset,
            unknown,
        }))
    }

    fn build<'py>(&self, cls: &Bound<'py, PyAny>, value: u128) -> PyResult<Bound<'py, PyAny>> {
        let py = cls.py();
        let int = value.into_pyobject(py)?;
        // SAFETY: `cls` is exactly uuid.UUID (checked at resolve time); tp_alloc returns a
        // zeroed, GC-tracked instance whose two object slots we fill with owned references.
        unsafe {
            let tp = cls.as_ptr().cast::<ffi::PyTypeObject>();
            let alloc = (*tp).tp_alloc.unwrap_or(ffi::PyType_GenericAlloc);
            let obj = Bound::from_owned_ptr_or_err(py, alloc(tp, 0))?;
            let base = obj.as_ptr().cast::<u8>();
            *base.offset(self.int_offset).cast::<*mut ffi::PyObject>() = int.into_ptr();
            *base
                .offset(self.is_safe_offset)
                .cast::<*mut ffi::PyObject>() = self.unknown.clone_ref(py).into_ptr();
            gc_untrack(&obj);
            Ok(obj)
        }
    }
}

/// Python objects needed while decoding, looked up once per interpreter.
pub struct PyCache {
    uuid_cls: Py<PyAny>,
    uuid_layout: Option<UuidLayout>,
    uuid_kwnames: Py<PyTuple>,
    decimal_cls: Py<PyAny>,
    json_decode: Py<PyAny>,
    utc: Py<PyTzInfo>,
}

static CACHE: PyOnceLock<PyCache> = PyOnceLock::new();

thread_local! {
    static NUMERIC_BUF: std::cell::RefCell<String> = const { std::cell::RefCell::new(String::new()) };
}

pub fn cache(py: Python<'_>) -> PyResult<&PyCache> {
    CACHE.get_or_try_init(py, || {
        let uuid_cls = py.import("uuid")?.getattr("UUID")?;
        Ok(PyCache {
            uuid_layout: UuidLayout::resolve(&uuid_cls)?,
            uuid_cls: uuid_cls.unbind(),
            uuid_kwnames: PyTuple::new(py, [intern!(py, "int")])?.unbind(),
            decimal_cls: py.import("decimal")?.getattr("Decimal")?.unbind(),
            // A long-lived Decoder, as msgspec recommends for repeated decoding.
            json_decode: py
                .import("msgspec.json")?
                .getattr("Decoder")?
                .call0()?
                .getattr("decode")?
                .unbind(),
            utc: PyTzInfo::utc(py)?.to_owned().unbind(),
        })
    })
}

fn malformed(codec: Codec, len: usize) -> String {
    format!("malformed binary value for {codec:?} ({len} bytes)")
}

fn fixed<const N: usize>(codec: Codec, raw: &[u8]) -> Result<[u8; N], String> {
    raw.try_into().map_err(|_| malformed(codec, raw.len()))
}

/// The text of a `"char"` value: `chr(byte)`, and `""` for the byte 0 (PostgreSQL's text
/// output for `'\0'::"char"`). A byte ≥ 128 also maps to `chr(byte)`, the Latin-1 code
/// point, although PostgreSQL itself prints such a byte as an octal escape.
pub fn char_text(byte: u8, buf: &mut [u8; 4]) -> &str {
    if byte == 0 {
        ""
    } else {
        char::from(byte).encode_utf8(buf)
    }
}

/// Decode UTF-8 straight into a Python str (CPython validates; no Rust-side pass).
pub fn py_str<'py>(py: Python<'py>, raw: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    // SAFETY: pointer/length come from a live slice; NULL `errors` means "strict".
    unsafe {
        let ptr = ffi::PyUnicode_DecodeUTF8(
            raw.as_ptr().cast::<c_char>(),
            raw.len() as ffi::Py_ssize_t,
            std::ptr::null(),
        );
        Bound::from_owned_ptr_or_err(py, ptr)
    }
}

/// Call `callable(*args, **dict(zip(kwnames, args[nargs:])))` via vectorcall, without
/// building an argument tuple or kwargs dict. `args[0]` must be a spare slot (it lets
/// CPython prepend `self` for bound methods without copying).
///
/// # Safety
/// Every pointer in `args[1..]` must be a valid, live Python object and `kwnames` must be
/// NULL or a tuple of `str` whose length is `args.len() - 1 - nargs`.
pub unsafe fn vectorcall<'py>(
    py: Python<'py>,
    callable: &Bound<'py, PyAny>,
    args: &mut [*mut ffi::PyObject],
    nargs: usize,
    kwnames: *mut ffi::PyObject,
) -> PyResult<Bound<'py, PyAny>> {
    // SAFETY: `args` is a live, mutable slice, so `add(1)` stays within it (or one past its
    // end) and the spare `args[0]` is ours for PY_VECTORCALL_ARGUMENTS_OFFSET to overwrite;
    // per this function's contract `args[1..]` holds live objects and `kwnames` matches them.
    unsafe {
        let res = ffi::PyObject_Vectorcall(
            callable.as_ptr(),
            args.as_mut_ptr().add(1),
            nargs | ffi::PY_VECTORCALL_ARGUMENTS_OFFSET,
            kwnames,
        );
        Bound::from_owned_ptr_or_err(py, res)
    }
}

/// Render a binary NUMERIC as a decimal string (`Decimal(str)` / msgspec input).
pub fn numeric_to_string(raw: &[u8]) -> Result<String, String> {
    let mut out = String::new();
    write_numeric(raw, &mut out)?;
    Ok(out)
}

/// Append the decimal rendering of a binary NUMERIC to `out`.
pub fn write_numeric(raw: &[u8], out: &mut String) -> Result<(), String> {
    let bad = || malformed(Codec::Numeric, raw.len());
    if raw.len() < 8 {
        return Err(bad());
    }
    let ndigits = u16::from_be_bytes([raw[0], raw[1]]) as usize;
    let weight = i16::from_be_bytes([raw[2], raw[3]]) as i64;
    let sign = u16::from_be_bytes([raw[4], raw[5]]);
    let dscale = u16::from_be_bytes([raw[6], raw[7]]) as usize;
    if raw.len() != 8 + 2 * ndigits {
        return Err(bad());
    }
    let special = match sign {
        0xC000 => "NaN",
        0xD000 => "Infinity",
        0xF000 => "-Infinity",
        0x0000 | 0x4000 => "",
        _ => return Err(bad()),
    };
    if !special.is_empty() {
        out.push_str(special);
        return Ok(());
    }
    let digit = |k: i64| -> u16 {
        if k < 0 || k as usize >= ndigits {
            0
        } else {
            let i = 8 + 2 * k as usize;
            u16::from_be_bytes([raw[i], raw[i + 1]])
        }
    };
    // Base-10000 digit → 4 ASCII digits.
    let push4 = |out: &mut String, d: u16| {
        let b = [
            b'0' + (d / 1000) as u8,
            b'0' + (d / 100 % 10) as u8,
            b'0' + (d / 10 % 10) as u8,
            b'0' + (d % 10) as u8,
        ];
        out.extend(b.iter().map(|&c| c as char));
    };
    out.reserve(ndigits * 4 + 4);
    if sign == 0x4000 {
        out.push('-');
    }
    if weight < 0 {
        out.push('0');
    } else {
        for k in 0..=weight {
            let d = digit(k);
            if k == 0 {
                // Leading group without zero padding.
                let start = out.len();
                push4(out, d);
                let pad = out[start..]
                    .bytes()
                    .take(3)
                    .take_while(|&c| c == b'0')
                    .count();
                out.drain(start..start + pad);
            } else {
                push4(out, d);
            }
        }
    }
    if dscale > 0 {
        out.push('.');
        let start = out.len();
        let groups = dscale.div_ceil(4) as i64;
        for p in 1..=groups {
            push4(out, digit(weight + p));
        }
        out.truncate(start + dscale);
    }
    Ok(())
}

pub fn read_i64(codec: Codec, raw: &[u8]) -> Result<i64, String> {
    Ok(match codec {
        Codec::Int2 => i64::from(i16::from_be_bytes(fixed(codec, raw)?)),
        Codec::Int4 => i64::from(i32::from_be_bytes(fixed(codec, raw)?)),
        Codec::Int8 => i64::from_be_bytes(fixed(codec, raw)?),
        Codec::Oid => i64::from(u32::from_be_bytes(fixed(codec, raw)?)),
        _ => unreachable!("read_i64 called for {codec:?}"),
    })
}

/// How a cell should be materialised for its target.
#[derive(Clone, Copy)]
pub struct Target<'a, 'py> {
    /// Field is `float` but the column is an integer type.
    pub int_as_float: bool,
    /// Field is `str` but the column is json/jsonb: the value is the raw JSON text.
    pub json_as_text: bool,
    /// Field-specific JSON decoder for json/jsonb columns: a callable taking bytes
    /// (`msgspec.json.Decoder(T).decode`, `TypeAdapter(T).validate_json`).
    pub json_decoder: Option<&'a Bound<'py, PyAny>>,
}

impl Target<'_, '_> {
    pub const PLAIN: Self = Target {
        int_as_float: false,
        json_as_text: false,
        json_decoder: None,
    };
}

/// Decode one non-NULL cell. Errors are plain strings; callers attach column context.
pub fn decode<'py>(
    py: Python<'py>,
    cache: &PyCache,
    codec: Codec,
    raw: &[u8],
    target: Target<'_, 'py>,
) -> Result<Bound<'py, PyAny>, DecodeFailure> {
    Ok(match codec {
        Codec::Void => py.None().into_bound(py),
        Codec::Bool => {
            let [b] = fixed::<1>(codec, raw)?;
            pyo3::types::PyBool::new(py, b != 0).to_owned().into_any()
        }
        Codec::Int2 | Codec::Int4 | Codec::Int8 | Codec::Oid => {
            let v = read_i64(codec, raw)?;
            if target.int_as_float {
                PyFloat::new(py, v as f64).into_any()
            } else {
                v.into_pyobject(py)?.into_any()
            }
        }
        Codec::Float4 => {
            PyFloat::new(py, f64::from(f32::from_be_bytes(fixed(codec, raw)?))).into_any()
        }
        Codec::Float8 => PyFloat::new(py, f64::from_be_bytes(fixed(codec, raw)?)).into_any(),
        Codec::Text => py_str(py, raw)?,
        Codec::Char => {
            let [b] = fixed::<1>(codec, raw)?;
            PyString::new(py, char_text(b, &mut [0; 4])).into_any()
        }
        Codec::Bytea => PyBytes::new(py, raw).into_any(),
        Codec::Uuid => {
            let n = u128::from_be_bytes(fixed(codec, raw)?);
            if let Some(layout) = &cache.uuid_layout {
                return Ok(layout.build(cache.uuid_cls.bind(py), n)?);
            }
            let int = n.into_pyobject(py)?;
            let mut args = [std::ptr::null_mut(), int.as_ptr()];
            // SAFETY: `int` outlives the call; kwnames is the 1-tuple ("int",).
            unsafe {
                vectorcall(
                    py,
                    cache.uuid_cls.bind(py),
                    &mut args,
                    0,
                    cache.uuid_kwnames.as_ptr(),
                )?
            }
        }
        Codec::Date => {
            let d = pgtime::ymd_from_pg_days(i32::from_be_bytes(fixed(codec, raw)?))?;
            PyDate::new(py, d.year, d.month, d.day)?.into_any()
        }
        Codec::Time => {
            let t = pgtime::hmsu_from_usecs(i64::from_be_bytes(fixed(codec, raw)?));
            PyTime::new(py, t.hour, t.minute, t.second, t.micro, None)?.into_any()
        }
        Codec::Timestamp | Codec::TimestampTz => {
            let (d, t) = pgtime::split_pg_timestamp(i64::from_be_bytes(fixed(codec, raw)?))?;
            let utc = cache.utc.bind(py);
            let tz = (codec == Codec::TimestampTz).then_some(utc);
            PyDateTime::new(
                py, d.year, d.month, d.day, t.hour, t.minute, t.second, t.micro, tz,
            )?
            .into_any()
        }
        Codec::Numeric => {
            // Reused buffer: no Rust allocation per value; vectorcall: no argument tuple.
            let text = NUMERIC_BUF.with_borrow_mut(|buf| {
                buf.clear();
                write_numeric(raw, buf).map(|()| PyString::new(py, buf))
            })?;
            let mut args = [std::ptr::null_mut(), text.as_ptr()];
            // SAFETY: `text` outlives the call; no keyword arguments.
            let decimal = unsafe {
                vectorcall(
                    py,
                    cache.decimal_cls.bind(py),
                    &mut args,
                    1,
                    std::ptr::null_mut(),
                )?
            };
            gc_untrack(&decimal);
            decimal
        }
        Codec::Json | Codec::Jsonb => {
            let text = if codec == Codec::Jsonb {
                match raw.split_first() {
                    Some((1, rest)) => rest,
                    _ => return Err(malformed(codec, raw.len()).into()),
                }
            } else {
                raw
            };
            if target.json_as_text {
                py_str(py, text)?
            } else {
                let bytes = PyBytes::new(py, text);
                match target.json_decoder {
                    Some(decoder) => decoder.call1((bytes,))?,
                    None => cache.json_decode.bind(py).call1((bytes,))?,
                }
            }
        }
    })
}

/// Either a Python exception raised while building the value, or a format/range problem.
pub enum DecodeFailure {
    Py(PyErr),
    Invalid(String),
}

impl From<PyErr> for DecodeFailure {
    fn from(e: PyErr) -> Self {
        Self::Py(e)
    }
}

impl From<std::convert::Infallible> for DecodeFailure {
    fn from(e: std::convert::Infallible) -> Self {
        match e {}
    }
}

impl From<String> for DecodeFailure {
    fn from(e: String) -> Self {
        Self::Invalid(e)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn numeric(ndigits: &[u16], weight: i16, neg: bool, dscale: u16) -> Vec<u8> {
        let mut v = Vec::new();
        v.extend((ndigits.len() as u16).to_be_bytes());
        v.extend(weight.to_be_bytes());
        v.extend((if neg { 0x4000u16 } else { 0 }).to_be_bytes());
        v.extend(dscale.to_be_bytes());
        for d in ndigits {
            v.extend(d.to_be_bytes());
        }
        v
    }

    #[test]
    fn numeric_rendering() {
        assert_eq!(numeric_to_string(&numeric(&[], 0, false, 0)).unwrap(), "0");
        assert_eq!(
            numeric_to_string(&numeric(&[], 0, false, 2)).unwrap(),
            "0.00"
        );
        assert_eq!(
            numeric_to_string(&numeric(&[12, 3400], 0, false, 2)).unwrap(),
            "12.34"
        );
        assert_eq!(
            numeric_to_string(&numeric(&[1, 0, 5], 1, true, 4)).unwrap(),
            "-10000.0005"
        );
        assert_eq!(
            numeric_to_string(&numeric(&[5], -2, false, 8)).unwrap(),
            "0.00000005"
        );
        assert_eq!(
            numeric_to_string(&numeric(&[1], 2, false, 0)).unwrap(),
            "100000000"
        );
    }
}
