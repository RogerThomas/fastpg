//! Python query parameters → PostgreSQL binary format.
//!
//! Python values are converted to [`Param`] when a query is submitted and encoded by xitca
//! in the query's I/O task, once the statement's parameter types are known.
//! Encoding is type-directed: each `Param` checks the server-declared parameter type and
//! fails with a precise [`ParamError`] instead of letting the server guess.

use std::cell::RefCell;
use std::error::Error;
use std::fmt;
use std::rc::Rc;

use bytes::{BufMut, BytesMut};
use pyo3::exceptions::{PyOverflowError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::sync::critical_section::with_critical_section;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDate, PyDateAccess, PyDateTime, PyDelta, PyDeltaAccess, PyDict,
    PyFloat, PyInt, PyList, PyMemoryView, PyString, PyTime, PyTimeAccess, PyTuple, PyType,
    PyTzInfo, PyTzInfoAccess,
};
use pyo3::{PyTypeCheck, ffi, intern};
use xitca_postgres::types::{IsNull, Kind, ToSql, Type, to_sql_checked};

use crate::errors::py_err;
use crate::pgtime::{PG_EPOCH_UNIX_DAYS, USECS_PER_DAY, USECS_PER_SEC, unix_days_from_civil};

#[derive(Debug)]
pub enum Param {
    Null,
    Bool(bool),
    Int(i64),
    /// `str(int)` of an int outside the 64-bit range (for numeric and float parameters).
    BigInt(String),
    Float(f64),
    Str(String),
    Bytes(Vec<u8>),
    Uuid(u128),
    /// Days since 2000-01-01.
    Date(i32),
    /// Microseconds since midnight.
    Time(i64),
    /// Naive timestamp, µs since 2000-01-01.
    Timestamp(i64),
    /// UTC instant, µs since 2000-01-01 00:00 UTC.
    TimestampTz(i64),
    /// `str(decimal.Decimal)`.
    Decimal(String),
    /// Pre-serialised JSON (from dict / list via msgspec).
    Json(Vec<u8>),
    /// A list or tuple for an array parameter: one value per element.
    Array(Vec<Param>),
    /// A list for a parameter of a statement this query prepares, whose type isn't known
    /// yet: converted both ways, it is sent as an array or as JSON, as the server declares.
    List(Box<UntypedList>),
    /// Any other value (not `None` or a `str`) for a parameter of a statement this query
    /// prepares: converted both ways too, it is sent as JSON if the server declares json or
    /// jsonb, and as itself otherwise.
    Untyped(Box<Untyped>),
}

/// Both encodings of a list, or why each is impossible.
#[derive(Debug)]
pub struct UntypedList {
    array: Result<Vec<Param>, String>,
    json: Result<Vec<u8>, String>,
}

/// A value and its JSON (see [`json_value`]), or why each is impossible.
#[derive(Debug)]
pub struct Untyped {
    value: Result<Param, String>,
    json: Result<Vec<u8>, String>,
}

impl Param {
    fn describe(&self) -> &'static str {
        match self {
            Self::Null => "None",
            Self::Bool(_) => "bool",
            Self::Int(_) | Self::BigInt(_) => "int",
            Self::Float(_) => "float",
            Self::Str(_) => "str",
            Self::Bytes(_) => "bytes",
            Self::Uuid(_) => "uuid.UUID",
            Self::Date(_) => "datetime.date",
            Self::Time(_) => "datetime.time",
            Self::Timestamp(_) => "naive datetime.datetime",
            Self::TimestampTz(_) => "aware datetime.datetime",
            Self::Decimal(_) => "decimal.Decimal",
            Self::Json(_) => "dict/list (JSON)",
            Self::Array(_) => "list/tuple (array)",
            // As the same list is described once the statement is prepared.
            Self::List(_) => "dict/list (JSON)",
            Self::Untyped(untyped) => match &untyped.value {
                Ok(value) => value.describe(),
                Err(_) => "value",
            },
        }
    }
}

pub(crate) struct PyTypes {
    pub(crate) uuid_cls: Py<PyAny>,
    /// Slot offset of `uuid.UUID.int`, to read it without the attribute protocol.
    uuid_int_offset: Option<ffi::Py_ssize_t>,
    pub(crate) decimal_cls: Py<PyAny>,
    enum_cls: Py<PyAny>,
    json_encode: Py<PyAny>,
    /// `msgspec.to_builtins`: a value as the dicts, lists and scalars its JSON is made of.
    to_builtins: Py<PyAny>,
    /// The metaclass of msgspec Structs.
    struct_meta: Py<PyAny>,
    /// `msgspec.UNSET`: a Struct field left out of the JSON.
    unset: Py<PyAny>,
    /// `datetime.timezone.utc`: aware datetimes in UTC need no `utcoffset()` call.
    utc: Py<PyAny>,
}

static TYPES: PyOnceLock<PyTypes> = PyOnceLock::new();

pub(crate) fn types(py: Python<'_>) -> PyResult<&PyTypes> {
    TYPES.get_or_try_init(py, || {
        let uuid_cls = py.import("uuid")?.getattr("UUID")?;
        Ok(PyTypes {
            uuid_int_offset: crate::decode::member_offset(&uuid_cls, "int")?,
            uuid_cls: uuid_cls.unbind(),
            decimal_cls: py.import("decimal")?.getattr("Decimal")?.unbind(),
            enum_cls: py.import("enum")?.getattr("Enum")?.unbind(),
            // A long-lived Encoder (reuses its internal buffer), as msgspec recommends.
            json_encode: py
                .import("msgspec.json")?
                .getattr("Encoder")?
                .call0()?
                .getattr("encode")?
                .unbind(),
            to_builtins: py.import("msgspec")?.getattr("to_builtins")?.unbind(),
            struct_meta: py
                .import("msgspec")?
                .getattr("Struct")?
                .get_type()
                .into_any()
                .unbind(),
            unset: py.import("msgspec")?.getattr("UNSET")?.unbind(),
            utc: PyTzInfo::utc(py)?.to_owned().into_any().unbind(),
        })
    })
}

/// The slot offsets of a Struct type's fields, if they are all plain object slots.
type StructSlots = Option<Rc<[ffi::Py_ssize_t]>>;

/// How many Struct types each thread remembers in `STRUCT_SLOTS`. Structs of other types are
/// searched by `refused`.
const STRUCT_SLOTS_CAP: usize = 64;

thread_local! {
    /// Struct types seen by `may_be_refused`, with their `StructSlots`. Each type is kept
    /// alive by a reference that is never released, so its pointer can't be reused.
    static STRUCT_SLOTS: RefCell<Vec<(*mut ffi::PyTypeObject, StructSlots)>> =
        const { RefCell::new(Vec::new()) };
}

/// The `StructSlots` of `cls`, a Struct type (see `member_offset`).
fn struct_slots(cls: &Bound<'_, PyType>) -> PyResult<StructSlots> {
    let ptr = cls.as_type_ptr();
    let cached = STRUCT_SLOTS.with_borrow(|cache| {
        cache
            .iter()
            .find(|(t, _)| *t == ptr)
            .map(|(_, slots)| slots.clone())
    });
    if let Some(slots) = cached {
        return Ok(slots);
    }
    let fields = cls.getattr(intern!(cls.py(), "__struct_fields__"))?;
    let slots = fields
        .cast::<PyTuple>()?
        .iter()
        .map(|name| crate::decode::member_offset(cls.as_any(), name.cast::<PyString>()?.to_str()?))
        .collect::<PyResult<Option<Vec<_>>>>()?
        .map(Rc::from);
    STRUCT_SLOTS.with_borrow_mut(|cache| {
        if cache.len() < STRUCT_SLOTS_CAP {
            cache.push((cls.clone().into_ptr().cast(), slots.clone()));
        }
    });
    Ok(slots)
}

impl PyTypes {
    /// The 128-bit value of a `uuid.UUID` (or subclass) instance.
    pub(crate) fn uuid_value(&self, obj: &Bound<'_, PyAny>) -> PyResult<u128> {
        let py = obj.py();
        if let Some(offset) = self.uuid_int_offset
            && obj.get_type().is(self.uuid_cls.bind(py))
        {
            // SAFETY: `obj` is exactly uuid.UUID, whose `int` member descriptor published
            // this object-slot offset (see `member_offset`).
            let slot = unsafe {
                *obj.as_ptr()
                    .cast::<u8>()
                    .offset(offset)
                    .cast::<*mut ffi::PyObject>()
            };
            if !slot.is_null() {
                // SAFETY: a non-NULL slot holds a valid object reference.
                return unsafe { Bound::from_borrowed_ptr(py, slot) }.extract::<u128>();
            }
        }
        obj.getattr(intern!(py, "int"))?.extract::<u128>()
    }

    /// µs since 2000-01-01 of a datetime: the wall-clock time, and for an aware datetime
    /// the UTC instant (`utcoffset()` is only called for zones other than UTC).
    pub(crate) fn datetime_usecs(
        &self,
        dt: &Bound<'_, PyDateTime>,
    ) -> PyResult<(i64, Option<i64>)> {
        let naive = pg_days(dt) * USECS_PER_DAY + time_usecs(dt);
        let Some(tz) = dt.get_tzinfo() else {
            return Ok((naive, None));
        };
        if tz.is(self.utc.bind(dt.py())) {
            return Ok((naive, Some(naive)));
        }
        let offset = dt.call_method0(intern!(dt.py(), "utcoffset"))?;
        Ok(match offset.cast::<PyDelta>() {
            Ok(offset) => (naive, Some(naive - delta_usecs(offset))),
            // `utcoffset()` returned None: the datetime is naive after all.
            Err(_) => (naive, None),
        })
    }
}

fn delta_usecs(d: &Bound<'_, PyDelta>) -> i64 {
    i64::from(d.get_days()) * USECS_PER_DAY
        + i64::from(d.get_seconds()) * USECS_PER_SEC
        + i64::from(d.get_microseconds())
}

pub(crate) fn pg_days(d: &impl PyDateAccess) -> i64 {
    unix_days_from_civil(i64::from(d.get_year()), d.get_month(), d.get_day()) - PG_EPOCH_UNIX_DAYS
}

fn time_usecs(t: &impl PyTimeAccess) -> i64 {
    (i64::from(t.get_hour()) * 3600 + i64::from(t.get_minute()) * 60 + i64::from(t.get_second()))
        * USECS_PER_SEC
        + i64::from(t.get_microsecond())
}

fn param_error(py: Python<'_>, index: usize, msg: impl fmt::Display) -> PyErr {
    py_err(
        py,
        "ParameterError",
        format!("parameter ${}: {msg}", index + 1),
    )
}

/// `ty`: the parameter's type, when the statement is already prepared (see [`convert`]).
pub fn from_py(obj: &Bound<'_, PyAny>, index: usize, ty: Option<&Type>) -> PyResult<Param> {
    let py = obj.py();
    convert_param(obj, ty).map_err(|e| match e {
        // As in bulk writes: a ParameterError naming the parameter, caused by the Python
        // exception (an unencodable str, a value msgspec can't serialise as JSON, ...).
        ConvertError::Py(e) => {
            let err = param_error(py, index, &e);
            err.set_cause(py, Some(e));
            err
        }
        ConvertError::Invalid(msg) => param_error(py, index, msg),
    })
}

/// [`convert`] for a query parameter, except that a json or jsonb parameter takes any value as
/// [`json_value`] does, as in bulk writes. Of a statement not prepared yet, a value is also
/// kept as its JSON, unless it is `None` or a `str`, which both types take as they are, or a
/// dict or list, which `convert` already sends as JSON.
fn convert_param(obj: &Bound<'_, PyAny>, ty: Option<&Type>) -> Result<Param, ConvertError> {
    if ty.is_some_and(|ty| !is_json(ty))
        || obj.is_instance_of::<PyDict>()
        || obj.is_instance_of::<PyList>()
    {
        return convert(obj, ty);
    }
    let Some(json) = json_value(obj) else {
        return convert(obj, ty);
    };
    if ty.is_some() {
        return Ok(Param::Json(json?));
    }
    match (convert(obj, None), json) {
        // Neither, for an exception: raised as it always has been.
        (Err(ConvertError::Py(e)), Err(_)) => Err(ConvertError::Py(e)),
        (value, json) => Ok(Param::Untyped(Box::new(Untyped {
            value: value.map_err(ConvertError::message),
            json: json.map_err(|e| e.to_string()),
        }))),
    }
}

/// Why a Python value can't become a [`Param`]: a Python exception, or a message the caller
/// turns into a `ParameterError` naming the parameter (or the row and column).
pub enum ConvertError {
    Py(PyErr),
    Invalid(String),
}

impl From<PyErr> for ConvertError {
    fn from(e: PyErr) -> Self {
        Self::Py(e)
    }
}

/// `obj` as a `T`, if it is one. Unlike `cast`, a mismatch builds no error (which holds a
/// reference to `T`'s type object, so costs more than the type check itself).
fn as_type<'a, 'py, T: PyTypeCheck>(obj: &'a Bound<'py, PyAny>) -> Option<&'a Bound<'py, T>> {
    // SAFETY: `type_check` has just confirmed that `obj` is a `T`.
    T::type_check(obj).then(|| unsafe { obj.cast_unchecked::<T>() })
}

impl ConvertError {
    fn message(self) -> String {
        match self {
            Self::Py(e) => e.to_string(),
            Self::Invalid(message) => message,
        }
    }

    /// The error for element `index` of an array.
    fn in_element(self, index: usize) -> Self {
        match self {
            Self::Invalid(message) => Self::Invalid(format!("element {index}: {message}")),
            py => py,
        }
    }
}

/// [`from_py`] without the parameter position (shared with the bulk-write encoders).
///
/// `ty`, the declared type, decides only what a list becomes: an array for an array type,
/// JSON for any other. Unknown (`None`, a statement's first use), a list is converted both
/// ways and the declared type picks one when it is encoded.
pub fn convert(obj: &Bound<'_, PyAny>, ty: Option<&Type>) -> Result<Param, ConvertError> {
    let py = obj.py();
    if obj.is_none() {
        return Ok(Param::Null);
    }
    if let Some(b) = as_type::<PyBool>(obj) {
        return Ok(Param::Bool(b.is_true()));
    }
    if obj.is_instance_of::<PyInt>() {
        return match obj.extract::<i64>() {
            Ok(v) => Ok(Param::Int(v)),
            // Still fine for a numeric or float parameter: kept as its decimal digits
            // (`int.__repr__`, which a subclass can't change). Past
            // `sys.get_int_max_str_digits()` Python refuses to convert it.
            Err(e) if e.is_instance_of::<PyOverflowError>(py) => py
                .get_type::<PyInt>()
                .call_method1(intern!(py, "__repr__"), (obj,))
                .and_then(|digits| Ok(Param::BigInt(digits.extract::<String>()?)))
                .map_err(|e| ConvertError::Invalid(format!("int too large to encode: {e}"))),
            Err(e) => Err(e.into()),
        };
    }
    if let Some(s) = as_type::<PyString>(obj) {
        return Ok(Param::Str(s.to_str()?.to_owned()));
    }
    if let Some(f) = as_type::<PyFloat>(obj) {
        return Ok(Param::Float(f.value()));
    }
    if let Some(b) = as_type::<PyBytes>(obj) {
        return Ok(Param::Bytes(b.as_bytes().to_vec()));
    }
    // An exact `uuid.UUID` (the usual key type) before the checks below, each of which
    // walks the type's MRO when it doesn't match; subclasses are caught further down.
    let types = types(py)?;
    if obj.get_type().is(types.uuid_cls.bind(py)) {
        return Ok(Param::Uuid(types.uuid_value(obj)?));
    }
    if let Some(b) = as_type::<PyByteArray>(obj) {
        return Ok(Param::Bytes(b.to_vec()));
    }
    if obj.is_instance_of::<PyMemoryView>() {
        return Ok(Param::Bytes(obj.extract::<Vec<u8>>()?));
    }
    // datetime is a subclass of date: check it first.
    if let Some(dt) = as_type::<PyDateTime>(obj) {
        return Ok(match types.datetime_usecs(dt)? {
            (_, Some(utc)) => Param::TimestampTz(utc),
            (naive, None) => Param::Timestamp(naive),
        });
    }
    if let Some(d) = as_type::<PyDate>(obj) {
        return Ok(Param::Date(pg_days(d) as i32));
    }
    if let Some(t) = as_type::<PyTime>(obj) {
        if t.get_tzinfo().is_some() {
            return Err(ConvertError::Invalid(
                "timezone-aware time (timetz) is not supported".into(),
            ));
        }
        return Ok(Param::Time(time_usecs(t)));
    }
    if obj.is_instance(types.uuid_cls.bind(py))? {
        return Ok(Param::Uuid(types.uuid_value(obj)?));
    }
    if obj.is_instance(types.decimal_cls.bind(py))? {
        return Ok(Param::Decimal(obj.str()?.to_str()?.to_owned()));
    }
    if obj.is_instance(types.enum_cls.bind(py))? {
        return convert(&obj.getattr("value")?, ty);
    }
    if obj.is_instance_of::<PyDict>() {
        return Ok(Param::Json(json_encode(obj)?));
    }
    if obj.is_instance_of::<PyList>() {
        return convert_list(obj, ty);
    }
    if obj.is_instance_of::<PyTuple>() {
        return Ok(Param::Array(elements(obj, ty.and_then(element_type))?));
    }
    Err(ConvertError::Invalid(format!(
        "unsupported type {}",
        obj.get_type().name()?
    )))
}

/// The element type of an array type (or of a domain over one).
fn element_type(ty: &Type) -> Option<&Type> {
    match base_type(ty).kind() {
        Kind::Array(elem) => Some(elem),
        _ => None,
    }
}

fn is_json(ty: &Type) -> bool {
    matches!(*base_type(ty), Type::JSON | Type::JSONB)
}

fn convert_list(list: &Bound<'_, PyAny>, ty: Option<&Type>) -> Result<Param, ConvertError> {
    let Some(ty) = ty else {
        let array = elements(list, None).map_err(ConvertError::message);
        return match json_encode(list) {
            Ok(json) => Ok(Param::List(Box::new(UntypedList {
                array,
                json: Ok(json),
            }))),
            Err(e) if array.is_ok() => Ok(Param::List(Box::new(UntypedList {
                array,
                json: Err(e.to_string()),
            }))),
            // Neither: the error a JSON parameter has always had.
            Err(e) => Err(e.into()),
        };
    };
    match element_type(ty) {
        Some(elem) => Ok(Param::Array(elements(list, Some(elem))?)),
        None => Ok(Param::Json(json_encode(list)?)),
    }
}

/// The elements of a list or tuple, converted for elements of type `ty` (if known).
fn elements(seq: &Bound<'_, PyAny>, ty: Option<&Type>) -> Result<Vec<Param>, ConvertError> {
    let element = |(i, item): (usize, Bound<'_, PyAny>)| {
        let list = item.is_instance_of::<PyList>();
        let nested = list || item.is_instance_of::<PyTuple>();
        if nested && ty.is_some_and(|ty| !is_json(ty)) {
            return Err(ConvertError::Invalid(format!(
                "element {i}: multi-dimensional arrays are not supported (got a nested {})",
                item.get_type().name()?
            )));
        }
        if list && ty.is_none() {
            // Only an element of a json/jsonb array can be a list, so it is only encoded as
            // JSON (and its own elements aren't converted, level after level).
            return Ok(Param::List(Box::new(UntypedList {
                array: Err(format!(
                    "element {i}: multi-dimensional arrays are not supported (got a nested list)"
                )),
                json: json_encode(&item).map_err(|e| e.to_string()),
            })));
        }
        convert(&item, ty).map_err(|e| e.in_element(i))
    };
    match as_type::<PyList>(seq) {
        Some(list) => list.iter().enumerate().map(element).collect(),
        None => seq
            .cast::<PyTuple>()
            .map_err(PyErr::from)?
            .iter()
            .enumerate()
            .map(element)
            .collect(),
    }
}

/// The JSON a json or jsonb parameter or column takes for `obj`, the same for query
/// parameters and bulk writes: `None` for `None` (NULL) and for a `str`, which is JSON text
/// already and is sent as it is; anything else as msgspec encodes it (see `json_encode`).
pub fn json_value(obj: &Bound<'_, PyAny>) -> Option<PyResult<Vec<u8>>> {
    if obj.is_none() || obj.is_instance_of::<PyString>() {
        return None;
    }
    Some(json_encode(obj))
}

/// `obj` serialised as JSON by the shared `msgspec.json.Encoder`.
///
/// msgspec writes NaN and the infinities as `null`, which JSON has instead, and bytes as
/// base64 text; they are refused rather than stored as something else. The quick
/// `may_be_refused` looks for them first.
pub fn json_encode(obj: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    let py = obj.py();
    let types = types(py)?;
    let encoded = types.json_encode.bind(py).call1((obj,))?;
    let json = encoded.cast::<PyBytes>()?.as_bytes();
    if may_be_refused(types, obj)?
        && let Some((found, path)) = refused(types, obj)?
    {
        return Err(PyValueError::new_err(match found {
            NoJsonForm::NonFinite(value) => {
                format!("{value} at ${path} has no JSON form (it would be stored as null)")
            }
            NoJsonForm::Bytes(name) => {
                format!("{name} at ${path} has no JSON form: base64-encode it yourself")
            }
        }));
    }
    Ok(json.to_vec())
}

/// A value msgspec encodes as something else, found by `refused`.
enum NoJsonForm {
    /// NaN or an infinity (its repr), encoded as `null`.
    NonFinite(String),
    /// bytes, bytearray or memoryview (the type's name), encoded as base64 text.
    Bytes(String),
}

fn is_bytes(obj: &Bound<'_, PyAny>) -> bool {
    obj.is_instance_of::<PyBytes>()
        || obj.is_instance_of::<PyByteArray>()
        || obj.is_instance_of::<PyMemoryView>()
}

/// Whether `obj` may hold a NaN, an infinity or bytes: a quicker first pass than `refused`,
/// which is never wrong when it says no. It builds no paths, reads a Struct's fields from its
/// slots instead of converting the whole Struct with `to_builtins`, and looks at the usual
/// scalars without taking a reference to them.
fn may_be_refused(types: &PyTypes, obj: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = obj.py();
    // SAFETY: `obj` is live.
    if let Some(found) = unsafe { scalar_refused(obj.as_ptr()) } {
        return Ok(found);
    }
    // Each item is looked at in place if it's a scalar, else walked as a new reference. Every
    // `item` below is a live object, lent by the container being walked, which no Python code
    // can change before it is looked at or taken. This relies on `gil_used = true` (`lib.rs`):
    // revisit it in the free-threading audit.
    let item = |item: *mut ffi::PyObject| {
        // SAFETY: `item` is live (above).
        match unsafe { scalar_refused(item) } {
            Some(found) => Ok(found),
            // SAFETY: `item` is live (above).
            None => may_be_refused(types, &unsafe { Bound::from_borrowed_ptr(py, item) }),
        }
    };
    // SAFETY: reads the type of `obj`'s type.
    if unsafe { ffi::Py_TYPE(obj.get_type_ptr().cast()) } == types.struct_meta.as_ptr().cast()
        && let Some(offsets) = struct_slots(&obj.get_type())?
    {
        for &offset in offsets.iter() {
            // SAFETY: `obj`'s type has the fields whose member descriptors published these
            // offsets for object slots (see `member_offset`).
            let slot = unsafe {
                *obj.as_ptr()
                    .cast::<u8>()
                    .offset(offset)
                    .cast::<*mut ffi::PyObject>()
            };
            // An unset field is left to `refused`.
            if slot.is_null() || item(slot)? {
                return Ok(true);
            }
        }
        return Ok(false);
    }
    if let Some(dict) = as_type::<PyDict>(obj) {
        return with_critical_section(obj, || {
            let (mut pos, mut key, mut value) = (0, std::ptr::null_mut(), std::ptr::null_mut());
            // SAFETY: `dict` is a dict, held for the loop.
            while unsafe {
                ffi::PyDict_Next(dict.as_ptr(), &raw mut pos, &raw mut key, &raw mut value)
            } != 0
            {
                if item(key)? || item(value)? {
                    return Ok(true);
                }
            }
            Ok(false)
        });
    }
    if let Some(list) = as_type::<PyList>(obj) {
        return with_critical_section(obj, || {
            let mut i = 0;
            // SAFETY: `list` is a list, held for the loop; its length is read again for each
            // item.
            while i < unsafe { ffi::PyList_GET_SIZE(list.as_ptr()) } {
                // SAFETY: as above, `i` is in range.
                if item(unsafe { ffi::PyList_GET_ITEM(list.as_ptr(), i) })? {
                    return Ok(true);
                }
                i += 1;
            }
            Ok(false)
        });
    }
    if let Some(tuple) = as_type::<PyTuple>(obj) {
        for i in 0..tuple.len() {
            // SAFETY: `tuple` is a tuple and `i` is in range.
            if item(unsafe { ffi::PyTuple_GET_ITEM(tuple.as_ptr(), i.cast_signed()) })? {
                return Ok(true);
            }
        }
        return Ok(false);
    }
    if let Some(float) = as_type::<PyFloat>(obj) {
        return Ok(!float.value().is_finite());
    }
    if is_bytes(obj) {
        return Ok(true);
    }
    // Other values msgspec encodes as a single string or number.
    let ty = obj.get_type();
    if obj.is_instance_of::<PyString>()
        || obj.is_instance_of::<PyInt>()
        || obj.is(types.unset.bind(py))
        || ty.is(types.uuid_cls.bind(py))
        || ty.is(types.decimal_cls.bind(py))
        || obj.is_instance_of::<PyDate>()
        || obj.is_instance_of::<PyTime>()
        || obj.is_instance_of::<PyDelta>()
    {
        return Ok(false);
    }
    Ok(refused(types, obj)?.is_some())
}

/// Whether `obj` is a NaN, an infinity or bytes, if it is exactly one of the usual scalars
/// (`None`, `str`, `int`, `bool`, `float`, `bytes` or `bytearray`); `None` for anything else.
///
/// # Safety
/// `obj` must point to a live object.
#[inline]
unsafe fn scalar_refused(obj: *mut ffi::PyObject) -> Option<bool> {
    // SAFETY: the caller's.
    unsafe {
        let ty = ffi::Py_TYPE(obj);
        if obj == ffi::Py_None()
            || ty == &raw mut ffi::PyUnicode_Type
            || ty == &raw mut ffi::PyLong_Type
            || ty == &raw mut ffi::PyBool_Type
        {
            return Some(false);
        }
        if ty == &raw mut ffi::PyBytes_Type || ty == &raw mut ffi::PyByteArray_Type {
            return Some(true);
        }
        (ty == &raw mut ffi::PyFloat_Type).then(|| !ffi::PyFloat_AS_DOUBLE(obj).is_finite())
    }
}

/// The first NaN, infinity or bytes in `obj`, and its path below `obj` (`.key[0]`). Dicts
/// (their keys too), lists and tuples are walked as they are; other objects (Structs,
/// dataclasses, enums, ...) as the builtins msgspec encodes them as.
fn refused(types: &PyTypes, obj: &Bound<'_, PyAny>) -> PyResult<Option<(NoJsonForm, String)>> {
    let py = obj.py();
    if let Some(float) = as_type::<PyFloat>(obj) {
        if float.value().is_finite() {
            return Ok(None);
        }
        let value = float.repr()?.to_string();
        return Ok(Some((NoJsonForm::NonFinite(value), String::new())));
    }
    if is_bytes(obj) {
        let name = obj.get_type().name()?.to_string();
        return Ok(Some((NoJsonForm::Bytes(name), String::new())));
    }
    if let Some(dict) = as_type::<PyDict>(obj) {
        for (key, value) in dict.iter() {
            let found = match refused(types, &key)? {
                None => refused(types, &value)?,
                found => found,
            };
            if let Some((found, path)) = found {
                let key = match as_type::<PyString>(&key) {
                    Some(key) => format!(".{}", key.to_str()?),
                    None => format!("[{key}]"),
                };
                return Ok(Some((found, key + &path)));
            }
        }
        return Ok(None);
    }
    let items = match (as_type::<PyList>(obj), as_type::<PyTuple>(obj)) {
        (Some(list), _) => list.iter().collect::<Vec<_>>(),
        (_, Some(tuple)) => tuple.iter().collect(),
        _ if obj.is_none() || obj.is_instance_of::<PyString>() || obj.is_instance_of::<PyInt>() => {
            return Ok(None);
        }
        _ => {
            // Bytes are kept as they are, to be found. Anything else comes back as one of the
            // builtins handled above, so this ends.
            let kwargs = PyDict::new(py);
            let bytes_types = (
                py.get_type::<PyBytes>(),
                py.get_type::<PyByteArray>(),
                py.get_type::<PyMemoryView>(),
            );
            kwargs.set_item(intern!(py, "builtin_types"), bytes_types)?;
            let builtins = types.to_builtins.bind(py).call((obj,), Some(&kwargs))?;
            return refused(types, &builtins);
        }
    };
    for (i, item) in items.iter().enumerate() {
        if let Some((found, path)) = refused(types, item)? {
            return Ok(Some((found, format!("[{i}]{path}"))));
        }
    }
    Ok(None)
}

/// The protocol counts a statement's parameters in 16 bits. Past that, the server would
/// prepare the statement but describe its parameters with a wrapped-around count.
const MAX_PARAMS: usize = u16::MAX as usize;

/// `types`: the parameter types of the statement, when it is already prepared.
pub fn from_py_tuple(params: &Bound<'_, PyTuple>, types: Option<&[Type]>) -> PyResult<Vec<Param>> {
    if params.len() > MAX_PARAMS {
        return Err(py_err(
            params.py(),
            "ParameterError",
            format!(
                "PostgreSQL takes at most {MAX_PARAMS} parameters per statement, got {}",
                params.len()
            ),
        ));
    }
    params
        .iter()
        .enumerate()
        .map(|(i, p)| from_py(&p, i, types.and_then(|types| types.get(i))))
        .collect()
}

/// Encoding failure, carried through xitca's boxed error and downcast on the way out.
#[derive(Debug)]
pub struct ParamError {
    pub index: usize,
    pub message: String,
}

impl fmt::Display for ParamError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "parameter ${}: {}", self.index + 1, self.message)
    }
}

impl Error for ParamError {}

/// A parameter paired with its position, so encoding errors can name it.
#[derive(Debug, Clone, Copy)]
pub struct Indexed<'a>(pub usize, pub &'a Param);

pub fn indexed(params: &[Param]) -> impl ExactSizeIterator<Item = Indexed<'_>> + Clone {
    params.iter().enumerate().map(|(i, p)| Indexed(i, p))
}

/// Domains are sent in their base type's binary format.
pub fn base_type(ty: &Type) -> &Type {
    match ty.kind() {
        Kind::Domain(inner) => base_type(inner),
        _ => ty,
    }
}

pub fn is_text(ty: &Type) -> bool {
    matches!(
        *ty,
        Type::TEXT | Type::VARCHAR | Type::BPCHAR | Type::NAME | Type::UNKNOWN
    ) || matches!(ty.kind(), Kind::Enum(_))
        || (matches!(ty.kind(), Kind::Simple) && ty.name() == "citext")
}

pub fn put_json(ty: &Type, json: &[u8], out: &mut BytesMut) {
    if *ty == Type::JSONB {
        out.put_u8(1);
    }
    out.put_slice(json);
}

type BoxError = Box<dyn Error + Sync + Send>;

impl ToSql for Indexed<'_> {
    fn to_sql(&self, declared: &Type, out: &mut BytesMut) -> Result<IsNull, BoxError> {
        let Indexed(index, param) = *self;
        let ty = base_type(declared);
        // Errors name the declared type, and a domain's base type too.
        let type_name = if ty == declared {
            ty.name().to_owned()
        } else {
            format!("{} (a domain over {})", declared.name(), ty.name())
        };
        let fail = |message: String| -> BoxError { Box::new(ParamError { index, message }) };
        let mismatch = || {
            fail(format!(
                "cannot encode {} as PostgreSQL type {type_name}",
                param.describe()
            ))
        };
        let range = |what: &str| {
            fail(format!(
                "{what} out of range for PostgreSQL type {type_name}"
            ))
        };

        match param {
            Param::Null => return Ok(IsNull::Yes),
            Param::Bool(b) if *ty == Type::BOOL => out.put_u8(u8::from(*b)),
            Param::Int(v) => match *ty {
                Type::INT2 => out.put_i16(i16::try_from(*v).map_err(|_| range("int"))?),
                Type::INT4 => out.put_i32(i32::try_from(*v).map_err(|_| range("int"))?),
                Type::INT8 => out.put_i64(*v),
                Type::OID => out.put_u32(u32::try_from(*v).map_err(|_| range("int"))?),
                Type::FLOAT4 => out.put_f32(*v as f32),
                Type::FLOAT8 => out.put_f64(*v as f64),
                Type::NUMERIC => encode_numeric(&v.to_string(), out).map_err(fail)?,
                _ => return Err(mismatch()),
            },
            Param::BigInt(digits) => match *ty {
                Type::INT2 | Type::INT4 | Type::INT8 | Type::OID => return Err(range("int")),
                Type::FLOAT4 | Type::FLOAT8 => {
                    // Correctly rounded, like Python's `float(int)`; past f64's range it is
                    // `inf`, where Python raises OverflowError.
                    let v = digits.parse::<f64>().map_err(|e| fail(e.to_string()))?;
                    match float4(v) {
                        Some(f) if *ty == Type::FLOAT4 && f.is_finite() => out.put_f32(f),
                        _ if *ty == Type::FLOAT8 && v.is_finite() => out.put_f64(v),
                        _ => return Err(range("int")),
                    }
                }
                Type::NUMERIC => encode_numeric(digits, out).map_err(fail)?,
                _ => return Err(mismatch()),
            },
            Param::Float(v) => match *ty {
                Type::FLOAT4 => out.put_f32(float4(*v).ok_or_else(|| range("float"))?),
                Type::FLOAT8 => out.put_f64(*v),
                Type::NUMERIC => encode_numeric(&float_repr(*v), out).map_err(fail)?,
                _ => return Err(mismatch()),
            },
            Param::Str(s) => match *ty {
                Type::JSON | Type::JSONB => put_json(ty, s.as_bytes(), out),
                Type::UUID => out.put_u128(
                    parse_uuid(s).ok_or_else(|| fail(format!("invalid UUID string {s:?}")))?,
                ),
                Type::NUMERIC => encode_numeric(s, out).map_err(fail)?,
                Type::CHAR => out.put_u8(char_byte(s).ok_or_else(|| {
                    fail(format!(
                        "\"char\" takes a str of at most one character below U+0100, got {s:?}"
                    ))
                })?),
                _ if is_text(ty) => out.put_slice(s.as_bytes()),
                _ => return Err(mismatch()),
            },
            Param::Bytes(b) if *ty == Type::BYTEA => out.put_slice(b),
            Param::Uuid(u) if *ty == Type::UUID => out.put_u128(*u),
            Param::Date(d) if *ty == Type::DATE => out.put_i32(*d),
            Param::Time(t) if *ty == Type::TIME => out.put_i64(*t),
            Param::Timestamp(t) if *ty == Type::TIMESTAMP => out.put_i64(*t),
            Param::TimestampTz(t) if *ty == Type::TIMESTAMPTZ => out.put_i64(*t),
            Param::Timestamp(_) if *ty == Type::TIMESTAMPTZ => {
                return Err(fail(
                    "naive datetime passed for a timestamptz parameter; attach a tzinfo".into(),
                ));
            }
            Param::TimestampTz(_) if *ty == Type::TIMESTAMP => {
                return Err(fail(
                    "aware datetime passed for a timestamp (without time zone) parameter".into(),
                ));
            }
            Param::Decimal(s) if *ty == Type::NUMERIC => encode_numeric(s, out).map_err(fail)?,
            Param::Json(j) if matches!(*ty, Type::JSON | Type::JSONB) => put_json(ty, j, out),
            Param::Array(items) => match element_type(ty) {
                Some(elem) => encode_array(index, items, elem, out)?,
                None => return Err(mismatch()),
            },
            Param::List(list) => match (element_type(ty), &list.array, &list.json) {
                (Some(elem), Ok(items), _) => encode_array(index, items, elem, out)?,
                (Some(_), Err(message), _) => return Err(fail(message.clone())),
                (None, _, Ok(json)) if is_json(ty) => put_json(ty, json, out),
                (None, _, Err(message)) if is_json(ty) => return Err(fail(message.clone())),
                (None, _, _) => return Err(mismatch()),
            },
            Param::Untyped(untyped) => match (&untyped.value, &untyped.json) {
                (_, Ok(json)) if is_json(ty) => put_json(ty, json, out),
                (_, Err(message)) if is_json(ty) => return Err(fail(message.clone())),
                (Ok(value), _) => return Indexed(index, value).to_sql(declared, out),
                (Err(message), _) => return Err(fail(message.clone())),
            },
            _ => return Err(mismatch()),
        }
        Ok(IsNull::No)
    }

    fn accepts(_: &Type) -> bool {
        // Checked precisely in `to_sql`, which produces better error messages.
        true
    }

    to_sql_checked!();
}

/// A one-dimensional array of `items` (empty: no dimensions), each encoded as a parameter of
/// type `elem` would be. Errors name the element.
fn encode_array(
    index: usize,
    items: &[Param],
    elem: &Type,
    out: &mut BytesMut,
) -> Result<(), BoxError> {
    let fail = |message: String| -> BoxError { Box::new(ParamError { index, message }) };
    let len = i32::try_from(items.len()).map_err(|_| fail("too many array elements".into()))?;
    // ndim, has-NULLs flag (set below), element type, then the one dimension: length and
    // lower bound.
    out.put_i32(i32::from(len > 0));
    let flags_at = out.len();
    out.put_i32(0);
    out.put_u32(elem.oid());
    if len > 0 {
        out.put_i32(len);
        out.put_i32(1);
    }
    for (i, item) in items.iter().enumerate() {
        if matches!(item, Param::Array(_) | Param::List(_)) && !is_json(elem) {
            return Err(fail(format!(
                "element {i}: multi-dimensional arrays are not supported (got a nested list)"
            )));
        }
        let len_at = out.len();
        out.put_i32(0);
        let len = match Indexed(index, item).to_sql(elem, out) {
            Ok(IsNull::Yes) => {
                out[flags_at..flags_at + 4].copy_from_slice(&1i32.to_be_bytes());
                -1
            }
            Ok(IsNull::No) => i32::try_from(out.len() - len_at - 4)
                .map_err(|_| fail(format!("element {i} is too large")))?,
            Err(e) => {
                return Err(match e.downcast::<ParamError>() {
                    Ok(p) => fail(format!("element {i}: {}", p.message)),
                    Err(e) => e,
                });
            }
        };
        out[len_at..len_at + 4].copy_from_slice(&len.to_be_bytes());
    }
    Ok(())
}

/// `v` as a float4, unless it is finite but beyond float4's range: PostgreSQL's float8 to
/// float4 cast refuses those too, rather than rounding them to infinity or zero.
pub fn float4(v: f64) -> Option<f32> {
    let f = v as f32;
    (!v.is_finite() || (f.is_finite() && (f != 0.0 || v == 0.0))).then_some(f)
}

fn float_repr(v: f64) -> String {
    if v.is_nan() {
        "NaN".into()
    } else if v.is_infinite() {
        if v > 0.0 {
            "Infinity".into()
        } else {
            "-Infinity".into()
        }
    } else {
        // Rust's Display is the shortest round-trip representation, like Python's repr.
        format!("{v}")
    }
}

/// The single byte of a `"char"` parameter, the inverse of `decode::char_text`: `""` is the
/// byte 0 and one character is its code point, if below U+0100 (so bytes ≥ 128 round-trip
/// as `chr(byte)`).
fn char_byte(s: &str) -> Option<u8> {
    let mut chars = s.chars();
    match (chars.next(), chars.next()) {
        (None, _) => Some(0),
        (Some(c), None) => u8::try_from(c).ok(),
        (Some(_), Some(_)) => None,
    }
}

fn parse_uuid(s: &str) -> Option<u128> {
    let hex: String = s.chars().filter(|c| *c != '-').collect();
    let hex = hex
        .strip_prefix('{')
        .and_then(|h| h.strip_suffix('}'))
        .unwrap_or(&hex);
    (hex.len() == 32)
        .then(|| u128::from_str_radix(hex, 16).ok())
        .flatten()
}

/// Encode a decimal string (`Decimal.__str__` syntax, incl. exponents) as binary NUMERIC.
pub fn encode_numeric(s: &str, out: &mut BytesMut) -> Result<(), String> {
    let invalid = || format!("invalid numeric value {s:?}");
    let header = |out: &mut BytesMut, ndigits: u16, weight: i16, sign: u16, dscale: u16| {
        out.put_u16(ndigits);
        out.put_i16(weight);
        out.put_u16(sign);
        out.put_u16(dscale);
    };
    let t = s.trim();
    match t {
        "NaN" | "nan" | "sNaN" | "-NaN" => {
            header(out, 0, 0, 0xC000, 0);
            return Ok(());
        }
        "Infinity" | "inf" | "+Infinity" => {
            header(out, 0, 0, 0xD000, 0);
            return Ok(());
        }
        "-Infinity" | "-inf" => {
            header(out, 0, 0, 0xF000, 0);
            return Ok(());
        }
        _ => {}
    }
    let (negative, body) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    let (mantissa, exp) = match body.find(['e', 'E']) {
        Some(i) => (
            &body[..i],
            body[i + 1..].parse::<i64>().map_err(|_| invalid())?,
        ),
        None => (body, 0),
    };
    let (int_part, frac_part) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    if int_part.is_empty() && frac_part.is_empty()
        || !int_part
            .bytes()
            .chain(frac_part.bytes())
            .all(|b| b.is_ascii_digit())
    {
        return Err(invalid());
    }
    // value = digits * 10^exp10, where digit i (from the left) stands for 10^(power0 - i).
    let (int_digits, frac_digits) = (int_part.as_bytes(), frac_part.as_bytes());
    let count = int_digits.len() + frac_digits.len();
    let digit = |i: usize| match int_digits.get(i) {
        Some(d) => d - b'0',
        None => frac_digits[i - int_digits.len()] - b'0',
    };
    let exp10 = exp
        .checked_sub(frac_digits.len() as i64)
        .ok_or_else(invalid)?;
    let dscale = u16::try_from((-exp10).max(0)).map_err(|_| invalid())?;
    let (Some(first), Some(last)) = (
        (0..count).find(|&i| digit(i) != 0),
        (0..count).rev().find(|&i| digit(i) != 0),
    ) else {
        header(out, 0, 0, 0, dscale); // zero
        return Ok(());
    };
    let power0 = (count as i64 - 1).checked_add(exp10).ok_or_else(invalid)?;
    // Base-10000 digit groups: group g holds the decimal digits for 10^(4g) .. 10^(4g + 3).
    let top = (power0 - first as i64).div_euclid(4);
    let bottom = (power0 - last as i64).div_euclid(4);
    let weight = i16::try_from(top).map_err(|_| format!("numeric value {s:?} is out of range"))?;
    let ndigits = u16::try_from(top - bottom + 1).map_err(|_| invalid())?;
    header(
        out,
        ndigits,
        weight,
        if negative { 0x4000 } else { 0 },
        dscale,
    );
    let start = out.len();
    out.put_bytes(0, 2 * usize::from(ndigits));
    for i in first..=last {
        let power = power0 - i as i64;
        let group = power.div_euclid(4);
        let at = start + 2 * (top - group) as usize;
        let value = u16::from_be_bytes([out[at], out[at + 1]])
            + u16::from(digit(i)) * 10u16.pow(power.rem_euclid(4) as u32);
        out[at..at + 2].copy_from_slice(&value.to_be_bytes());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::decode::numeric_to_string;

    fn roundtrip(s: &str) -> String {
        let mut b = BytesMut::new();
        encode_numeric(s, &mut b).unwrap();
        numeric_to_string(&b).unwrap()
    }

    #[test]
    fn numeric_roundtrips() {
        for (input, expected) in [
            ("0", "0"),
            ("0.00", "0.00"),
            ("12.34", "12.34"),
            ("-10000.0005", "-10000.0005"),
            ("0.00000005", "0.00000005"),
            ("100000000", "100000000"),
            ("1E+3", "1000"),
            ("1.5E-7", "0.00000015"),
            ("123456789.987654321", "123456789.987654321"),
            ("-0.5", "-0.5"),
            ("0.0001", "0.0001"),
            ("10000", "10000"),
            ("12345678.9", "12345678.9"),
            ("1E+10", "10000000000"),
            ("-1.23E-10", "-0.000000000123"),
            ("0E-8", "0.00000000"),
            ("-0", "0"),
            ("9999.9999", "9999.9999"),
            (
                "99999999999999999999.99999999",
                "99999999999999999999.99999999",
            ),
            ("NaN", "NaN"),
            ("-Infinity", "-Infinity"),
        ] {
            assert_eq!(roundtrip(input), expected, "{input}");
        }
    }

    #[test]
    fn array_encoding() {
        let mut b = BytesMut::new();
        let items = [Param::Int(1), Param::Null, Param::Int(-2)];
        encode_array(0, &items, &Type::INT4, &mut b).unwrap();
        let one: &[u8] = &1i32.to_be_bytes();
        let minus_two: &[u8] = &(-2i32).to_be_bytes();
        assert_eq!(
            &b[..],
            crate::decode::tests::array(&[(3, 1)], &[Some(one), None, Some(minus_two)])
        );
        b.clear();
        encode_array(0, &[], &Type::INT4, &mut b).unwrap();
        assert_eq!(&b[..], crate::decode::tests::array(&[], &[]));
        let err = encode_array(0, &[Param::Str("x".into())], &Type::INT4, &mut b).unwrap_err();
        assert_eq!(
            err.to_string(),
            "parameter $1: element 0: cannot encode str as PostgreSQL type int4"
        );
    }

    #[test]
    fn uuid_parsing() {
        assert_eq!(
            parse_uuid("28dd2c79-55ce-9264-5624-0b2ff0100bde"),
            Some(0x28dd2c7955ce926456240b2ff0100bde)
        );
        assert_eq!(parse_uuid("nope"), None);
    }
}
