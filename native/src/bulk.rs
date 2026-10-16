//! Bulk writes: `execute_many` / `insert_many` (one prepared statement, many parameter rows,
//! pipelined) and `copy_in` (binary `COPY ... FROM STDIN`).
//!
//! The Python wrapper (`fastpg/_writes.py`) drives one of these in chunks:
//!
//! 1. `Connection.execute_many()` / `copy_in()` return a [`Bulk`] handle and spawn an I/O task.
//!    The task takes the connection's gate *exclusively* (nothing else is pipelined into the
//!    bulk transaction or COPY stream; concurrent queries wait), then, in one round trip,
//!    probes whether a transaction block is open and prepares the statement (or describes the
//!    COPY target's columns). `ready` resolves.
//! 2. Python calls [`Bulk::feed`] with lists of rows. Rows are read and encoded to binary on
//!    the loop thread (the GIL is held for one chunk at a time) using the server-declared
//!    types, then handed to the I/O task, which pipelines them. Each chunk's `ack` future
//!    resolves once the server has answered for it (`execute_many`) or the data has been
//!    written to the socket (COPY), which bounds memory.
//! 3. [`Bulk::finish`] ends the input; `done` resolves to the affected/copied row count.
//!    [`Bulk::abort`] (or dropping the handle) rolls everything back.
//!
//! Atomicity: outside a transaction block the rows run inside `BEGIN` .. `COMMIT` (COPY is a
//! single statement anyway); inside one, inside `SAVEPOINT fastpg_bulk` .. `RELEASE`. On any
//! error or abort the savepoint is rolled back, so the caller's transaction is left exactly as
//! it was before the call. See `notes/writes.md` for the protocol reasoning.

use std::collections::VecDeque;
use std::future::{Future, poll_fn};
use std::pin::Pin;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock, PoisonError};
use std::task::Poll;

use bytes::{BufMut, Bytes, BytesMut};
use pyo3::ffi;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{
    PyByteArray, PyBytes, PyDate, PyDateTime, PyDict, PyList, PyMapping, PySequence, PyString,
    PyTuple,
};
use tokio::sync::mpsc;
use xitca_postgres::dev::{r#Copy as _, Encode as _};
use xitca_postgres::error::{DbError, UnexpectedMessage};
use xitca_postgres::iter::AsyncLendingIterator;
use xitca_postgres::types::{IsNull, ToSql, Type, to_sql_checked};
use xitca_postgres::{Client, Execute, RowSimpleStream, Statement};

use crate::connection::{Outcome, Request, Shared, plain_request};
use crate::errors::{QueryError, py_err, with_row_index};
use crate::net::{Cycle, IoWatch};
use crate::params::{self, ConvertError, Indexed, ParamError};

/// Detects an open transaction block without side effects: a transaction-local setting
/// survives into the next `Sync` only inside an explicit transaction (each implicit one ends
/// at its `Sync`). Two simple queries, pipelined.
const PROBE_SET: &str = "SELECT pg_catalog.set_config('fastpg.in_transaction', 'on', true)";
const PROBE_GET: &str = "SELECT pg_catalog.current_setting('fastpg.in_transaction', true)";
const SAVEPOINT: &str = "SAVEPOINT fastpg_bulk";
const RELEASE: &str = "RELEASE SAVEPOINT fastpg_bulk";
const ROLLBACK_TO: &str = "ROLLBACK TO SAVEPOINT fastpg_bulk; RELEASE SAVEPOINT fastpg_bulk";

/// Binary COPY header: signature, flags, header-extension length.
const COPY_HEADER: &[u8] = b"PGCOPY\n\xff\r\n\0\0\0\0\0\0\0\0\0";
/// Bytes of COPY data handed to xitca but not yet written to the socket before a chunk is
/// acknowledged (xitca's write buffer is unbounded).
const COPY_HIGH_WATER: u64 = 4 << 20;

const NULL: u32 = u32::MAX;

/// What `execute()` returns for both simple queries and bound statements
/// (`ResultFuture<RowAffected>`; xitca doesn't export the names).
type ExecFuture = <&'static str as Execute<&'static Client>>::ExecuteOutput;

// ---------------------------------------------------------------------------------------
// Value encoding (loop thread)
// ---------------------------------------------------------------------------------------

/// Encoder shortcuts for the commonest (Python type, PostgreSQL type) pairs; everything else
/// goes through `params::convert` + the regular `ToSql` encoders, so semantics match query
/// parameters exactly.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Fast {
    Bool,
    Int2,
    Int4,
    Int8,
    Float4,
    Float8,
    Text,
    Bytea,
    Json,
    Uuid,
    Numeric,
    Date,
    Timestamp,
    TimestampTz,
    Other,
}

/// How to encode one column (or statement parameter).
pub struct ColumnCodec {
    ty: Type,
    fast: Fast,
    /// `column "age"` / `parameter $2`, for error messages.
    label: String,
}

impl ColumnCodec {
    fn new(ty: &Type, label: String) -> Self {
        let ty = params::base_type(ty).clone();
        let fast = match ty {
            Type::BOOL => Fast::Bool,
            Type::INT2 => Fast::Int2,
            Type::INT4 => Fast::Int4,
            Type::INT8 => Fast::Int8,
            Type::FLOAT4 => Fast::Float4,
            Type::FLOAT8 => Fast::Float8,
            Type::BYTEA => Fast::Bytea,
            Type::JSON | Type::JSONB => Fast::Json,
            Type::UUID => Fast::Uuid,
            Type::NUMERIC => Fast::Numeric,
            Type::DATE => Fast::Date,
            Type::TIMESTAMP => Fast::Timestamp,
            Type::TIMESTAMPTZ => Fast::TimestampTz,
            _ if params::is_text(&ty) => Fast::Text,
            _ => Fast::Other,
        };
        Self { ty, fast, label }
    }

    fn range_error(&self) -> ConvertError {
        ConvertError::Invalid(format!(
            "int out of range for PostgreSQL type {}",
            self.ty.name()
        ))
    }

    /// Append `obj`'s binary encoding to `out`; `Ok(false)` means SQL NULL (nothing written).
    fn encode(&self, obj: &Bound<'_, PyAny>, out: &mut BytesMut) -> Result<bool, ConvertError> {
        if obj.is_none() {
            return Ok(false);
        }
        let p = obj.as_ptr();
        // SAFETY: the type checks only read the object's type pointer.
        let (exact_int, exact_float, exact_str) = unsafe {
            (
                ffi::PyLong_CheckExact(p) != 0,
                ffi::PyFloat_CheckExact(p) != 0,
                ffi::PyUnicode_CheckExact(p) != 0,
            )
        };
        match self.fast {
            Fast::Text if exact_str => {
                // SAFETY: checked to be exactly `str` above.
                let s = unsafe { obj.cast_unchecked::<PyString>() };
                out.put_slice(s.to_str()?.as_bytes());
                return Ok(true);
            }
            Fast::Int2 | Fast::Int4 | Fast::Int8 if exact_int => {
                let v = match obj.extract::<i64>() {
                    Ok(v) => v,
                    Err(_) => {
                        return Err(ConvertError::Invalid("int does not fit in 64 bits".into()));
                    }
                };
                match self.fast {
                    Fast::Int2 => out.put_i16(i16::try_from(v).map_err(|_| self.range_error())?),
                    Fast::Int4 => out.put_i32(i32::try_from(v).map_err(|_| self.range_error())?),
                    _ => out.put_i64(v),
                }
                return Ok(true);
            }
            Fast::Float8 if exact_float => {
                // SAFETY: `p` is an exact float.
                out.put_f64(unsafe { ffi::PyFloat_AS_DOUBLE(p) });
                return Ok(true);
            }
            Fast::Float4 if exact_float => {
                // SAFETY: `p` is an exact float.
                out.put_f32(unsafe { ffi::PyFloat_AS_DOUBLE(p) } as f32);
                return Ok(true);
            }
            Fast::Bool if obj.is_exact_instance_of::<pyo3::types::PyBool>() => {
                out.put_u8(u8::from(obj.is_truthy()?));
                return Ok(true);
            }
            Fast::Bytea if obj.is_exact_instance_of::<PyBytes>() => {
                // SAFETY: checked to be exactly `bytes` above.
                out.put_slice(unsafe { obj.cast_unchecked::<PyBytes>() }.as_bytes());
                return Ok(true);
            }
            // A `str` is taken as JSON text (as for query parameters); anything else that
            // msgspec can serialise (dict, list, nested Structs, numbers, ...) is encoded.
            Fast::Json if !obj.is_instance_of::<PyString>() => {
                let json = params::json_encode(obj).map_err(|e| {
                    ConvertError::Invalid(format!("cannot encode value as JSON: {e}"))
                })?;
                params::put_json(&self.ty, &json, out);
                return Ok(true);
            }
            Fast::Uuid => {
                let types = params::types(obj.py())?;
                if obj.get_type().is(types.uuid_cls.bind(obj.py())) {
                    out.put_u128(types.uuid_value(obj)?);
                    return Ok(true);
                }
            }
            Fast::Numeric => {
                let types = params::types(obj.py())?;
                if obj.get_type().is(types.decimal_cls.bind(obj.py())) {
                    params::encode_numeric(obj.str()?.to_str()?, out)
                        .map_err(ConvertError::Invalid)?;
                    return Ok(true);
                }
            }
            Fast::Date if obj.is_exact_instance_of::<PyDate>() => {
                // SAFETY: checked to be exactly `datetime.date` above.
                let date = unsafe { obj.cast_unchecked::<PyDate>() };
                out.put_i32(params::pg_days(date) as i32);
                return Ok(true);
            }
            Fast::Timestamp | Fast::TimestampTz if obj.is_exact_instance_of::<PyDateTime>() => {
                // SAFETY: checked to be exactly `datetime.datetime` above.
                let dt = unsafe { obj.cast_unchecked::<PyDateTime>() };
                // A naive datetime for timestamptz (or an aware one for timestamp) falls
                // through to the generic path, which explains the mismatch.
                match (self.fast, params::types(obj.py())?.datetime_usecs(dt)?) {
                    (Fast::Timestamp, (naive, None)) => {
                        out.put_i64(naive);
                        return Ok(true);
                    }
                    (Fast::TimestampTz, (_, Some(utc))) => {
                        out.put_i64(utc);
                        return Ok(true);
                    }
                    _ => {}
                }
            }
            _ => {}
        }
        let param = params::convert(obj)?;
        match Indexed(0, &param).to_sql(&self.ty, out) {
            Ok(IsNull::Yes) => Ok(false),
            Ok(IsNull::No) => Ok(true),
            Err(e) => Err(ConvertError::Invalid(match e.downcast::<ParamError>() {
                Ok(p) => p.message,
                Err(e) => e.to_string(),
            })),
        }
    }
}

// ---------------------------------------------------------------------------------------
// Row readers (loop thread)
// ---------------------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq)]
enum ReaderKind {
    /// tuple / list / any non-text sequence, positional.
    Sequence,
    /// dict / any mapping, by key.
    Mapping,
    /// msgspec Struct, by attribute (object slots read directly for the exact type).
    Struct,
    /// pydantic model, from its `__dict__`.
    Model,
}

/// How to pull column values out of one kind of row object. Built once per call in
/// `fastpg/_writes.py` (which also decides the column names).
#[pyclass(frozen, module = "fastpg._native")]
pub struct RowReader {
    kind: ReaderKind,
    /// Mapping keys or attribute names (interned), one per column.
    names: Vec<Py<PyString>>,
    /// Column names for error messages (empty: use the statement's labels).
    labels: Vec<String>,
    cls: Option<Py<PyAny>>,
    /// Struct slot offsets, when every field is a plain object slot.
    offsets: Option<Vec<ffi::Py_ssize_t>>,
    /// Mappings must have exactly these keys (columns were taken from the first row).
    exact: bool,
}

/// Why a row couldn't be encoded.
enum RowError {
    Py(PyErr),
    Row(String),
    Value(usize, ConvertError),
}

impl From<PyErr> for RowError {
    fn from(e: PyErr) -> Self {
        Self::Py(e)
    }
}

fn type_name(obj: &Bound<'_, PyAny>) -> String {
    obj.get_type()
        .name()
        .map_or_else(|_| "?".into(), |n| n.to_string())
}

#[pymethods]
impl RowReader {
    /// `kind`: 0 sequence, 1 mapping (keys `names`), 2 msgspec Struct, 3 pydantic model
    /// (attributes `names` of `row_type` instances). `labels` are the column names, for
    /// error messages. `exact`: mappings must have exactly the keys `names`.
    #[new]
    #[pyo3(signature = (kind, names, *, labels = Vec::new(), row_type = None, exact = false))]
    fn new(
        kind: u8,
        names: Vec<Bound<'_, PyString>>,
        labels: Vec<String>,
        row_type: Option<Bound<'_, PyAny>>,
        exact: bool,
    ) -> PyResult<Self> {
        let cls = row_type;
        let kind = match kind {
            0 => ReaderKind::Sequence,
            1 => ReaderKind::Mapping,
            2 => ReaderKind::Struct,
            3 => ReaderKind::Model,
            _ => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "invalid reader kind {kind}"
                )));
            }
        };
        if matches!(kind, ReaderKind::Struct | ReaderKind::Model) && cls.is_none() {
            return Err(pyo3::exceptions::PyValueError::new_err(
                "Struct and model readers need row_type",
            ));
        }
        let offsets = match (&cls, kind) {
            (Some(cls), ReaderKind::Struct) => names
                .iter()
                .map(|n| crate::decode::member_offset(cls, n.to_str()?))
                .collect::<PyResult<Option<Vec<_>>>>()?,
            _ => None,
        };
        Ok(Self {
            kind,
            names: names.into_iter().map(Bound::unbind).collect(),
            labels,
            cls: cls.map(Bound::unbind),
            offsets,
            exact,
        })
    }
}

impl RowReader {
    fn sequence() -> Self {
        Self {
            kind: ReaderKind::Sequence,
            names: Vec::new(),
            labels: Vec::new(),
            cls: None,
            offsets: None,
            exact: false,
        }
    }

    /// Values per row this reader produces (`None`: whatever the statement needs).
    fn width(&self) -> Option<usize> {
        (self.kind != ReaderKind::Sequence).then_some(self.names.len())
    }

    fn label(&self, col: usize, plan: &Plan) -> String {
        match self.labels.get(col) {
            Some(name) => format!("column {name:?}"),
            None => plan.columns[col].label.clone(),
        }
    }

    /// Call `f(column, value)` for each of the row's `ncols` values, in column order.
    fn read_row<'py>(
        &self,
        row: &Bound<'py, PyAny>,
        ncols: usize,
        mut f: impl FnMut(usize, &Bound<'py, PyAny>) -> Result<(), ConvertError>,
    ) -> Result<(), RowError> {
        let py = row.py();
        let mut emit = |col: usize, value: &Bound<'py, PyAny>| {
            f(col, value).map_err(|e| RowError::Value(col, e))
        };
        match self.kind {
            ReaderKind::Sequence => {
                let wrong_len =
                    |n: usize| RowError::Row(format!("expected {ncols} values, got {n}"));
                if let Ok(t) = row.cast::<PyTuple>() {
                    if t.len() != ncols {
                        return Err(wrong_len(t.len()));
                    }
                    for (i, value) in t.iter().enumerate() {
                        emit(i, &value)?;
                    }
                } else if let Ok(l) = row.cast::<PyList>() {
                    if l.len() != ncols {
                        return Err(wrong_len(l.len()));
                    }
                    for i in 0..ncols {
                        emit(i, &l.get_item(i)?)?;
                    }
                } else if row.is_instance_of::<PyString>()
                    || row.is_instance_of::<PyBytes>()
                    || row.is_instance_of::<PyByteArray>()
                {
                    return Err(RowError::Row(format!(
                        "expected a sequence of values, got {}",
                        type_name(row)
                    )));
                } else if let Ok(seq) = row.cast::<PySequence>() {
                    let n = seq.len()?;
                    if n != ncols {
                        return Err(wrong_len(n));
                    }
                    for i in 0..ncols {
                        emit(i, &seq.get_item(i)?)?;
                    }
                } else {
                    return Err(RowError::Row(format!(
                        "expected a sequence of values (tuple or list), got {}",
                        type_name(row)
                    )));
                }
            }
            ReaderKind::Mapping => {
                let missing = |i: usize| {
                    RowError::Row(format!(
                        "missing key {:?}",
                        self.labels.get(i).map_or("?", |s| s)
                    ))
                };
                if let Ok(d) = row.cast::<PyDict>() {
                    if self.exact && d.len() != ncols {
                        return Err(self.unexpected_keys(d));
                    }
                    for (i, key) in self.names.iter().enumerate() {
                        match d.get_item(key.bind(py))? {
                            Some(value) => emit(i, &value)?,
                            None => return Err(missing(i)),
                        }
                    }
                } else if let Ok(m) = row.cast::<PyMapping>() {
                    if self.exact && m.len()? != ncols {
                        return Err(RowError::Row(format!(
                            "expected exactly the keys {:?}",
                            self.labels
                        )));
                    }
                    for (i, key) in self.names.iter().enumerate() {
                        match m.get_item(key.bind(py)) {
                            Ok(value) => emit(i, &value)?,
                            Err(e) if e.is_instance_of::<pyo3::exceptions::PyKeyError>(py) => {
                                return Err(missing(i));
                            }
                            Err(e) => return Err(e.into()),
                        }
                    }
                } else {
                    return Err(RowError::Row(format!(
                        "expected a mapping, got {}",
                        type_name(row)
                    )));
                }
            }
            ReaderKind::Struct | ReaderKind::Model => {
                // `new` rejects Struct and Model readers without a row type.
                #[allow(clippy::expect_used)]
                let cls = self.cls.as_ref().expect("checked in new()").bind(py);
                if self.kind == ReaderKind::Struct
                    && row.get_type().is(cls)
                    && let Some(offsets) = &self.offsets
                {
                    for (i, offset) in offsets.iter().enumerate() {
                        // SAFETY: `row`'s type is exactly `cls`, whose member descriptors
                        // published these offsets for object slots (see `member_offset`).
                        let slot = unsafe {
                            *row.as_ptr()
                                .cast::<u8>()
                                .offset(*offset)
                                .cast::<*mut ffi::PyObject>()
                        };
                        if slot.is_null() {
                            return Err(RowError::Row(format!(
                                "attribute {:?} is not set",
                                self.names[i].bind(py)
                            )));
                        }
                        // SAFETY: a non-NULL slot holds a valid object reference.
                        let value = unsafe { Bound::from_borrowed_ptr(py, slot) };
                        emit(i, &value)?;
                    }
                    return Ok(());
                }
                if !row.is_instance(cls)? {
                    return Err(RowError::Row(format!(
                        "expected {}, got {} (rows must all be the same type)",
                        cls.getattr(intern!(py, "__qualname__"))?,
                        type_name(row)
                    )));
                }
                if self.kind == ReaderKind::Model {
                    let dict = row
                        .getattr(intern!(py, "__dict__"))?
                        .cast_into::<PyDict>()
                        .map_err(PyErr::from)?;
                    for (i, name) in self.names.iter().enumerate() {
                        let name = name.bind(py);
                        match dict.get_item(name)? {
                            Some(value) => emit(i, &value)?,
                            None => emit(i, &row.getattr(name)?)?,
                        }
                    }
                } else {
                    for (i, name) in self.names.iter().enumerate() {
                        emit(i, &row.getattr(name.bind(py))?)?;
                    }
                }
            }
        }
        Ok(())
    }

    fn unexpected_keys(&self, d: &Bound<'_, PyDict>) -> RowError {
        let extra: Vec<String> = d
            .keys()
            .iter()
            .filter_map(|k| k.extract::<String>().ok())
            .filter(|k| !self.labels.contains(k))
            .collect();
        if extra.is_empty() {
            let missing: Vec<&String> = self
                .labels
                .iter()
                .filter(|l| !d.contains(l.as_str()).unwrap_or(false))
                .collect();
            RowError::Row(format!("missing key(s) {missing:?}"))
        } else {
            RowError::Row(format!(
                "unexpected key(s) {extra:?} (columns are taken from the first row; pass \
                 columns= to select a subset)"
            ))
        }
    }
}

// ---------------------------------------------------------------------------------------
// Encoded chunks
// ---------------------------------------------------------------------------------------

/// Encoded parameter values for a chunk of `execute_many` rows.
struct ParamRows {
    ncols: usize,
    /// Index of the chunk's first row in the caller's input.
    first: usize,
    nrows: usize,
    data: BytesMut,
    /// (offset, len) per value, row-major; `len == NULL` marks SQL NULL.
    cells: Vec<(u32, u32)>,
}

impl ParamRows {
    /// The parameters of rows `r..r + count`, concatenated.
    fn rows(&self, r: usize, count: usize) -> impl ExactSizeIterator<Item = RawParam<'_>> + Clone {
        self.cells[r * self.ncols..(r + count) * self.ncols]
            .iter()
            .map(|&(start, len)| {
                RawParam((len != NULL).then(|| &self.data[start as usize..(start + len) as usize]))
            })
    }
}

/// An already-encoded binary parameter value.
#[derive(Debug, Clone, Copy)]
struct RawParam<'a>(Option<&'a [u8]>);

impl ToSql for RawParam<'_> {
    fn to_sql(
        &self,
        _: &Type,
        out: &mut BytesMut,
    ) -> Result<IsNull, Box<dyn std::error::Error + Sync + Send>> {
        match self.0 {
            None => Ok(IsNull::Yes),
            Some(raw) => {
                out.put_slice(raw);
                Ok(IsNull::No)
            }
        }
    }

    fn accepts(_: &Type) -> bool {
        // Encoded for exactly the statement's parameter types in `ColumnCodec::encode`.
        true
    }

    to_sql_checked!();
}

enum Msg {
    Rows(ParamRows, Arc<Request>),
    Copy {
        data: Bytes,
        rows: usize,
        ack: Arc<Request>,
    },
    Finish,
}

impl Msg {
    fn into_ack(self) -> Option<Arc<Request>> {
        match self {
            Self::Rows(_, ack) | Self::Copy { ack, .. } => Some(ack),
            Self::Finish => None,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum PlanKind {
    Execute,
    Copy,
}

/// Column encoders, known once the statement is prepared / the COPY target described.
struct Plan {
    kind: PlanKind,
    columns: Vec<ColumnCodec>,
}

// ---------------------------------------------------------------------------------------
// The Python-facing handle
// ---------------------------------------------------------------------------------------

/// Handle for one running bulk write (see the module docs).
#[pyclass(frozen, module = "fastpg._native")]
pub struct Bulk {
    tx: Mutex<Option<mpsc::UnboundedSender<Msg>>>,
    plan: Arc<OnceLock<Plan>>,
    reader: Py<RowReader>,
    /// Rows fed so far (loop thread only; atomics for `Sync`).
    fed: AtomicUsize,
    /// The connection, pumped after every call (that is how its tasks get polled).
    shared: Arc<Shared>,
}

impl RowError {
    fn into_pyerr(self, py: Python<'_>, index: usize, reader: &RowReader, plan: &Plan) -> PyErr {
        let (text, cause) = match self {
            Self::Py(e) => (format!("row {index}: {e}"), Some(e)),
            Self::Row(msg) => (format!("row {index}: {msg}"), None),
            Self::Value(col, ConvertError::Invalid(msg)) => (
                format!("row {index}, {}: {msg}", reader.label(col, plan)),
                None,
            ),
            Self::Value(col, ConvertError::Py(e)) => (
                format!("row {index}, {}: {e}", reader.label(col, plan)),
                Some(e),
            ),
        };
        let err = py_err(py, "ParameterError", text);
        if let Some(cause) = cause {
            err.set_cause(py, Some(cause));
        }
        with_row_index(py, err, index, None)
    }
}

impl Bulk {
    fn encode_params(
        &self,
        py: Python<'_>,
        plan: &Plan,
        reader: &RowReader,
        rows: &Bound<'_, PyList>,
        first: usize,
    ) -> PyResult<ParamRows> {
        let ncols = plan.columns.len();
        let mut out = ParamRows {
            ncols,
            first,
            nrows: 0,
            data: BytesMut::with_capacity(rows.len() * ncols * 8),
            cells: Vec::with_capacity(rows.len() * ncols),
        };
        for (i, row) in rows.iter().enumerate() {
            reader
                .read_row(&row, ncols, |col, value| {
                    let start = out.data.len();
                    let cell = if plan.columns[col].encode(value, &mut out.data)? {
                        (start as u32, (out.data.len() - start) as u32)
                    } else {
                        (0, NULL)
                    };
                    out.cells.push(cell);
                    Ok(())
                })
                .map_err(|e| e.into_pyerr(py, first + i, reader, plan))?;
            out.nrows += 1;
            if out.data.len() >= NULL as usize {
                return Err(py_err(
                    py,
                    "InterfaceError",
                    "chunk exceeds 4 GiB of parameter data",
                ));
            }
        }
        Ok(out)
    }

    fn encode_copy(
        &self,
        py: Python<'_>,
        plan: &Plan,
        reader: &RowReader,
        rows: &Bound<'_, PyList>,
        first: usize,
    ) -> PyResult<BytesMut> {
        let ncols = plan.columns.len();
        let mut out = BytesMut::with_capacity(rows.len() * (2 + ncols * 12));
        for (i, row) in rows.iter().enumerate() {
            out.put_i16(ncols as i16);
            reader
                .read_row(&row, ncols, |col, value| {
                    let at = out.len();
                    out.put_i32(-1);
                    if plan.columns[col].encode(value, &mut out)? {
                        let len = i32::try_from(out.len() - at - 4)
                            .map_err(|_| ConvertError::Invalid("value larger than 2 GiB".into()))?;
                        out[at..at + 4].copy_from_slice(&len.to_be_bytes());
                    }
                    Ok(())
                })
                .map_err(|e| e.into_pyerr(py, first + i, reader, plan))?;
        }
        Ok(out)
    }
}

#[pymethods]
impl Bulk {
    /// Encode `rows` (on this thread, GIL held) and queue them; `ack` resolves to None once
    /// the I/O task has dealt with them. Raises `ParameterError` (with `row_index`) for a row
    /// that can't be encoded; the caller must then `abort()`.
    fn feed(&self, py: Python<'_>, ack: Py<PyAny>, rows: &Bound<'_, PyList>) -> PyResult<()> {
        let Some(plan) = self.plan.get() else {
            return Err(py_err(py, "InterfaceError", "bulk write is not ready"));
        };
        let reader = self.reader.get();
        if let Some(width) = reader.width()
            && width != plan.columns.len()
        {
            return Err(py_err(
                py,
                "ParameterError",
                format!(
                    "rows provide {width} value(s) but the statement expects {}",
                    plan.columns.len()
                ),
            ));
        }
        let first = self.fed.load(Ordering::Relaxed);
        let request = plain_request(ack.clone_ref(py));
        let msg = match plan.kind {
            PlanKind::Execute => {
                Msg::Rows(self.encode_params(py, plan, reader, rows, first)?, request)
            }
            PlanKind::Copy => Msg::Copy {
                data: self.encode_copy(py, plan, reader, rows, first)?.freeze(),
                rows: rows.len(),
                ack: request,
            },
        };
        self.fed.store(first + rows.len(), Ordering::Relaxed);
        let sent = self
            .tx
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .as_ref()
            .is_some_and(|tx| tx.send(msg).is_ok());
        if !sent {
            // The I/O task is gone (it already reported why through `done`).
            ack.call_method1(py, intern!(py, "set_result"), (py.None(),))?;
        }
        self.shared.pump(py);
        Ok(())
    }

    /// No more rows: commit and resolve `done` with the row count.
    fn finish(&self, py: Python<'_>) {
        if let Some(tx) = self
            .tx
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take()
        {
            let _ = tx.send(Msg::Finish);
        }
        self.shared.pump(py);
    }

    /// Roll back everything (no-op after `finish()`); `done` resolves to None.
    fn abort(&self, py: Python<'_>) {
        drop(
            self.tx
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .take(),
        );
        self.shared.pump(py);
    }
}

struct Task {
    shared: Arc<Shared>,
    client: Arc<Client>,
    plan: Arc<OnceLock<Plan>>,
    rx: mpsc::UnboundedReceiver<Msg>,
    ready: Arc<Request>,
    done: Arc<Request>,
}

fn new_bulk(
    py: Python<'_>,
    shared: Arc<Shared>,
    client: Arc<Client>,
    reader: Option<Py<RowReader>>,
    ready: Py<PyAny>,
    done: Py<PyAny>,
) -> PyResult<(Bulk, Task)> {
    let (tx, rx) = mpsc::unbounded_channel();
    let plan = Arc::new(OnceLock::new());
    let reader = match reader {
        Some(reader) => reader,
        None => Py::new(py, RowReader::sequence())?,
    };
    let bulk = Bulk {
        tx: Mutex::new(Some(tx)),
        plan: plan.clone(),
        reader,
        fed: AtomicUsize::new(0),
        shared: shared.clone(),
    };
    let task = Task {
        shared,
        client,
        plan,
        rx,
        ready: plain_request(ready),
        done: plain_request(done),
    };
    Ok((bulk, task))
}

/// The statement(s) `execute_many` runs per row.
pub enum Statements {
    /// The caller's SQL, once per row (one statement per row: exact per-row counts and error
    /// positions; a chunk's statements share one `Sync`, see [`send_batch`]).
    PerRow(String),
    /// `<prefix> ($1, .., $width), (..), ..` covering `group` rows per statement (the last
    /// statements of a chunk cover one row each). Fewer, larger statements cost the server
    /// less per row than one statement per row.
    Values {
        prefix: String,
        width: usize,
        group: usize,
    },
}

impl Statements {
    fn sql(&self, rows: usize) -> String {
        match self {
            Self::PerRow(sql) => sql.clone(),
            Self::Values { prefix, width, .. } => {
                let mut sql = prefix.clone();
                for r in 0..rows {
                    sql.push_str(if r == 0 { "(" } else { ", (" });
                    for c in 0..*width {
                        if c > 0 {
                            sql.push_str(", ");
                        }
                        sql.push_str(&format!("${}", r * width + c + 1));
                    }
                    sql.push(')');
                }
                sql
            }
        }
    }

    fn group(&self) -> usize {
        match self {
            Self::PerRow(_) => 1,
            Self::Values { group, .. } => *group,
        }
    }
}

pub fn start_execute_many(
    py: Python<'_>,
    shared: Arc<Shared>,
    client: Arc<Client>,
    statements: Statements,
    reader: Option<Py<RowReader>>,
    ready: Py<PyAny>,
    done: Py<PyAny>,
) -> PyResult<Bulk> {
    let (bulk, task) = new_bulk(py, shared.clone(), client, reader, ready, done)?;
    let watch = DeadWatch::of(&task);
    shared.spawn(watch.run(execute_many_task(task, statements)));
    Ok(bulk)
}

#[allow(clippy::too_many_arguments)]
pub fn start_copy_in(
    py: Python<'_>,
    shared: Arc<Shared>,
    client: Arc<Client>,
    table: String,
    columns: Option<Vec<String>>,
    reader: Py<RowReader>,
    ready: Py<PyAny>,
    done: Py<PyAny>,
) -> PyResult<Bulk> {
    let (bulk, task) = new_bulk(py, shared.clone(), client, Some(reader), ready, done)?;
    let watch = DeadWatch::of(&task);
    shared.spawn(watch.run(copy_in_task(task, table, columns)));
    Ok(bulk)
}

// ---------------------------------------------------------------------------------------
// I/O tasks
// ---------------------------------------------------------------------------------------

pub fn quote_ident(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}

/// Read the probe's answer: is an explicit transaction block open?
async fn read_probe(
    query: impl Future<Output = Result<RowSimpleStream, xitca_postgres::Error>>,
) -> Result<bool, xitca_postgres::Error> {
    let mut stream = query.await?;
    let mut on = false;
    while let Some(row) = stream.try_next().await? {
        on = row.try_get(0)? == Some("on");
    }
    Ok(on)
}

/// Deallocate a statement we prepared for this call only (fire and forget).
fn close(client: &Client, stmt: Statement) {
    drop(stmt.into_guarded(client));
}

impl Task {
    fn post(&self, request: Arc<Request>, outcome: Outcome) {
        self.shared.post(request, outcome);
    }

    /// Setup failed before `ready`: report it there; `done` resolves to None.
    fn fail_setup(&self, e: QueryError) {
        self.post(self.ready.clone(), Outcome::Failed(e));
        self.post(self.done.clone(), Outcome::Done);
    }

    /// Acknowledge whatever is still queued, so no Python future is left pending.
    fn drain_input(&mut self) {
        self.rx.close();
        while let Ok(msg) = self.rx.try_recv() {
            if let Some(ack) = msg.into_ack() {
                self.shared.post(ack, Outcome::Done);
            }
        }
    }
}

/// The statements that run one chunk of rows, behind a single `Sync`: `rows / group`
/// statements of `group` rows each, then a one-row statement per row left over.
#[derive(Clone, Copy)]
struct Batch {
    /// Index of the chunk's first row in the caller's input.
    first: usize,
    rows: usize,
    group: usize,
}

impl Batch {
    fn full(self) -> usize {
        self.rows / self.group
    }

    fn statements(self) -> usize {
        self.full() + self.rows % self.group
    }

    /// Statement `i`'s rows: (index of its first row in the input, row count).
    fn statement(self, i: usize) -> (usize, usize) {
        let full = self.full();
        if i < full {
            (self.first + i * self.group, self.group)
        } else {
            (self.first + full * self.group + (i - full), 1)
        }
    }
}

/// What xitca appends to every request: `Sync`.
const SYNC: &[u8] = b"S\0\0\0\x04";

/// Pipeline every statement of `batch` behind one `Sync`, and return the future of the last
/// one (the only request xitca knows about; it receives the whole cycle).
///
/// xitca 0.4 ends every request with a `Sync`, which makes the server flush a
/// `ReadyForQuery` per statement, and the client handle a read per statement. So all but the
/// last statement are encoded by xitca, stripped of their `Sync` and sent one-way: the server
/// then answers the chunk in a few large writes. Per-statement results come from the socket
/// tap ([`IoWatch::take_cycle`]), since xitca only reports a cycle's last `CommandComplete`.
fn send_batch(
    client: &Client,
    stmt: &Statement,
    single: Option<&Statement>,
    rows: &ParamRows,
    batch: Batch,
) -> Result<ExecFuture, xitca_postgres::Error> {
    let statement = |i: usize| {
        let (start, count) = batch.statement(i);
        let stmt = if i < batch.full() {
            stmt
        } else {
            single.unwrap_or(stmt)
        };
        (stmt, rows.rows(start - batch.first, count))
    };
    let last = batch.statements() - 1;
    if last > 0 {
        client.send_one_way(|buf| {
            for i in 0..last {
                let (stmt, params) = statement(i);
                stmt.bind(params).encode(buf)?;
                if !buf.ends_with(SYNC) {
                    let unexpected = "xitca-postgres did not end a request with Sync";
                    return Err(std::io::Error::other(unexpected).into());
                }
                buf.truncate(buf.len() - SYNC.len());
            }
            Ok(())
        })?;
    }
    let (stmt, params) = statement(last);
    Ok(stmt.bind(params).execute(client))
}

/// The error of a batch, placed on its failing statement's row(s) when the tap saw how many
/// statements completed before it.
fn batch_error(batch: Batch, cycle: Option<Cycle>, e: xitca_postgres::Error) -> QueryError {
    let e = Box::new(e.into());
    match cycle {
        Some(cycle) if cycle.failed && cycle.completed < batch.statements() => {
            match batch.statement(cycle.completed) {
                (row, 1) => QueryError::AtRow(row, e),
                (first, count) => QueryError::InRows(first, count, e),
            }
        }
        _ => QueryError::InRows(batch.first, batch.rows, e),
    }
}

/// Stops the tap when the bulk task ends, however it ends (it is dropped if the connection
/// dies).
struct Collecting(Arc<IoWatch>);

impl Collecting {
    fn start(watch: Arc<IoWatch>) -> Self {
        watch.start_collecting();
        Self(watch)
    }
}

impl Drop for Collecting {
    fn drop(&mut self) {
        self.0.stop_collecting();
    }
}

enum Pending {
    /// One chunk of rows (one response cycle).
    Rows(Batch, ExecFuture),
    Control(ExecFuture),
    Ack(Arc<Request>),
}

/// The result of the oldest outstanding statement (never resolves for an empty queue).
fn poll_front(
    q: &mut VecDeque<Pending>,
) -> impl Future<Output = Result<u64, xitca_postgres::Error>> {
    poll_fn(move |cx| match q.front_mut() {
        Some(Pending::Rows(_, f) | Pending::Control(f)) => Pin::new(f).poll(cx),
        _ => Poll::Pending,
    })
}

/// Runs a bulk task until it finishes or the connection dies.
///
/// xitca 0.4 never fails requests still waiting when its driver goes away (see
/// `Shared::unless_dead`), so a bulk task blocked on the server would wait forever. If the
/// connection dies first, the task is dropped, which releases its exclusive gate and drops
/// any queued chunk acks (the Python feeder races each ack against `done`), and `ready` /
/// `done` fail with ConnectionClosedError. Posting to an already-resolved request is a no-op.
struct DeadWatch {
    shared: Arc<Shared>,
    ready: Arc<Request>,
    done: Arc<Request>,
}

impl DeadWatch {
    fn of(task: &Task) -> Self {
        Self {
            shared: task.shared.clone(),
            ready: task.ready.clone(),
            done: task.done.clone(),
        }
    }

    async fn run(self, work: impl Future<Output = ()>) {
        let finished = self
            .shared
            .unless_dead(async {
                work.await;
                Ok(())
            })
            .await;
        if finished.is_err() {
            self.shared
                .post(self.ready, Outcome::Failed(QueryError::Closed));
            self.shared
                .post(self.done, Outcome::Failed(QueryError::Closed));
        }
    }
}

async fn execute_many_task(mut task: Task, statements: Statements) {
    let shared = task.shared.clone();
    let _gate = shared.gate.write().await;
    let client = task.client.clone();

    // One round trip: transaction probe + prepare (fresh statements per call, so a stale
    // cached one can never fail half-way through and the cache isn't churned). Grouped
    // inserts also prepare a one-row statement for the rows left over at the end.
    let group = statements.group();
    let probe_set = PROBE_SET.execute(&*client);
    let probe_get = PROBE_GET.query(&*client);
    let group_sql = statements.sql(group);
    let single_sql = statements.sql(1);
    let prepared = Statement::named(&group_sql, &[]).execute(&*client);
    let prepared_single = (group > 1).then(|| Statement::named(&single_sql, &[]).execute(&*client));
    let set = probe_set.await;
    let in_txn = read_probe(probe_get).await;
    let stmt = prepared.await.map(|s| s.leak());
    let single = match prepared_single {
        Some(fut) => fut.await.map(|s| Some(s.leak())),
        None => Ok(None),
    };
    let (in_txn, stmt, single) = match (set.and(in_txn), stmt, single) {
        (Ok(in_txn), Ok(stmt), Ok(single)) => (in_txn, stmt, single),
        (probe, stmt, single) => {
            // Report the first failure (in send order); close whatever was prepared.
            let mut errors = Vec::new();
            if let Err(e) = probe {
                errors.push(e);
            }
            match stmt {
                Ok(s) => close(&client, s),
                Err(e) => errors.push(e),
            }
            match single {
                Ok(s) => s.into_iter().for_each(|s| close(&client, s)),
                Err(e) => errors.push(e),
            }
            // This arm only matches when at least one of the three is an `Err`.
            #[allow(clippy::expect_used)]
            let first = errors.into_iter().next().expect("one of them failed");
            return task.fail_setup(first.into());
        }
    };
    // Grouped statements repeat the row's parameters: encode with the first row's types.
    let width = stmt.params().len() / group;
    let columns = stmt.params()[..width]
        .iter()
        .enumerate()
        .map(|(i, ty)| ColumnCodec::new(ty, format!("parameter ${}", i + 1)))
        .collect();
    let _ = task.plan.set(Plan {
        kind: PlanKind::Execute,
        columns,
    });
    // Nothing of ours is outstanding now, so every cycle from here on answers our requests
    // in order (or is skipped as foreign): see `IoWatch::start_collecting`.
    let Some(watch) = shared.io.get().cloned() else {
        close(&client, stmt);
        single.into_iter().for_each(|s| close(&client, s));
        let unwatched = "connection has no socket watch".to_owned();
        return task.fail_setup(QueryError::Interface(unwatched));
    };
    let mut collecting = Some(Collecting::start(watch.clone()));
    task.post(task.ready.clone(), Outcome::Done);

    let (begin, end) = if in_txn {
        (SAVEPOINT, RELEASE)
    } else {
        ("BEGIN", "COMMIT")
    };
    let mut q: VecDeque<Pending> = VecDeque::new();
    let (mut began, mut ended, mut finished, mut aborted, mut failed) =
        (false, false, false, false, false);
    let mut total: u64 = 0;
    loop {
        while let Some(Pending::Ack(_)) = q.front() {
            if let Some(Pending::Ack(ack)) = q.pop_front() {
                task.post(ack, Outcome::Done);
            }
        }
        let input_open = !finished && !aborted;
        if q.is_empty() && !input_open {
            break;
        }
        enum Event {
            Result(Result<u64, xitca_postgres::Error>),
            Input(Option<Msg>),
        }
        let event = tokio::select! {
            biased;
            r = poll_front(&mut q), if !q.is_empty() => Event::Result(r),
            m = task.rx.recv(), if input_open => Event::Input(m),
        };
        match event {
            Event::Result(result) => {
                // Each of our requests is one cycle, in order: consume it even if unused.
                let cycle = collecting.as_ref().and_then(|_| watch.take_cycle());
                let error = match (q.pop_front(), result) {
                    (Some(Pending::Rows(batch, _)), Ok(_)) => match cycle {
                        Some(c) if !c.failed && c.completed == batch.statements() => {
                            total += c.affected;
                            None
                        }
                        // Can't happen unless the tap lost track: fail rather than miscount.
                        _ => Some(QueryError::Interface(format!(
                            "lost track of the server's responses (rows {}..{}); nothing was \
                             written",
                            batch.first,
                            batch.first + batch.rows - 1
                        ))),
                    },
                    (Some(Pending::Rows(batch, _)), Err(e)) => Some(batch_error(batch, cycle, e)),
                    (_, Err(e)) => Some(e.into()),
                    (_, Ok(_)) => None,
                };
                // Rows after the first failure fail too (aborted transaction): ignore them.
                if let Some(e) = error
                    && !failed
                    && !aborted
                {
                    failed = true;
                    collecting = None; // results are no longer needed
                    task.post(task.done.clone(), Outcome::Failed(e));
                }
            }
            Event::Input(Some(Msg::Rows(rows, ack))) => {
                if !failed && rows.nrows > 0 {
                    if !began {
                        q.push_back(Pending::Control(begin.execute(&*client)));
                        began = true;
                    }
                    let batch = Batch {
                        first: rows.first,
                        rows: rows.nrows,
                        group,
                    };
                    match send_batch(&client, &stmt, single.as_ref(), &rows, batch) {
                        Ok(fut) => q.push_back(Pending::Rows(batch, fut)),
                        Err(e) => {
                            failed = true;
                            collecting = None;
                            task.post(task.done.clone(), Outcome::Failed(e.into()));
                        }
                    }
                }
                q.push_back(Pending::Ack(ack));
            }
            Event::Input(Some(Msg::Finish)) => {
                finished = true;
                if began && !failed {
                    q.push_back(Pending::Control(end.execute(&*client)));
                    ended = true;
                }
            }
            Event::Input(Some(msg @ Msg::Copy { .. })) => {
                if let Some(ack) = msg.into_ack() {
                    q.push_back(Pending::Ack(ack));
                }
            }
            Event::Input(None) => aborted = true,
        }
    }

    drop(collecting);
    // Undo whatever didn't commit. A COMMIT in an aborted transaction already rolled back.
    let cleanup = match (began, in_txn) {
        (true, false) if !ended => Some("ROLLBACK"),
        (true, true) if failed || aborted => Some(ROLLBACK_TO),
        _ => None,
    };
    if let Some(sql) = cleanup {
        let _ = sql.execute(&*client).await;
    }
    close(&client, stmt);
    if let Some(single) = single {
        close(&client, single);
    }
    task.drain_input();
    if !failed {
        let outcome = if aborted {
            Outcome::Done
        } else {
            Outcome::Affected(total)
        };
        task.post(task.done.clone(), outcome);
    }
}

/// 0-based row of a COPY failure, from its `COPY t, line N` context.
fn copy_error_row(e: &xitca_postgres::Error) -> Option<usize> {
    let context = e.downcast_ref::<DbError>()?.where_()?;
    context.lines().find_map(|line| {
        let rest = line.strip_prefix("COPY ")?;
        let at = rest.find(", line ")? + ", line ".len();
        let digits: String = rest[at..]
            .chars()
            .take_while(char::is_ascii_digit)
            .collect();
        digits.parse::<usize>().ok()?.checked_sub(1)
    })
}

fn copy_error(e: xitca_postgres::Error) -> QueryError {
    match copy_error_row(&e) {
        Some(row) => QueryError::AtRow(row, Box::new(e.into())),
        None => e.into(),
    }
}

/// Write one frontend message (`tag`, length, body) straight into xitca's send buffer.
fn send_message(client: &Client, tag: u8, body: &[&[u8]]) -> Result<u64, xitca_postgres::Error> {
    let len: usize = 4 + body.iter().map(|b| b.len()).sum::<usize>();
    client.send_one_way(|buf| {
        buf.reserve(1 + len);
        buf.put_u8(tag);
        buf.put_i32(len as i32);
        for part in body {
            buf.put_slice(part);
        }
        Ok(())
    })?;
    Ok(1 + len as u64)
}

/// Wait until at most [`COPY_HIGH_WATER`] bytes of what we handed to xitca are unwritten.
///
/// Woken by `IoWatch::progress`, which fires after every socket write and on EOF or a fatal
/// I/O error. That is the only wake-up: the driver writes when the loop reports the socket
/// writable (`add_writer`), and a connection that dies meanwhile drops this task through
/// `DeadWatch`, so no timer (and no tokio runtime) is involved.
async fn wait_for_socket(watch: Option<&IoWatch>, base: u64, sent: u64, client: &Client) {
    let Some(watch) = watch else { return };
    let backlog = |written: u64| sent.saturating_sub(written.saturating_sub(base));
    loop {
        if backlog(watch.written()) <= COPY_HIGH_WATER || client.closed() {
            return;
        }
        let notified = watch.progress.notified();
        tokio::pin!(notified);
        notified.as_mut().enable();
        if backlog(watch.written()) <= COPY_HIGH_WATER {
            return;
        }
        notified.await;
    }
}

/// Next event of the COPY's response: `Ok` once complete, `Err` for an error (or, first, the
/// `CopyInResponse`, which xitca's simple-query stream reports as an unexpected message).
async fn next_copy_message(
    stream: &mut Option<RowSimpleStream>,
) -> Result<(), xitca_postgres::Error> {
    match stream {
        Some(stream) => stream.try_next().await.map(|_| ()),
        None => std::future::pending().await,
    }
}

async fn copy_in_task(mut task: Task, table: String, columns: Option<Vec<String>>) {
    let shared = task.shared.clone();
    let _gate = shared.gate.write().await;
    let client = task.client.clone();

    let select_list = match &columns {
        Some(columns) => columns
            .iter()
            .map(|c| quote_ident(c))
            .collect::<Vec<_>>()
            .join(", "),
        None => "*".into(),
    };
    let describe_sql = format!("SELECT {select_list} FROM {table} LIMIT 0");
    let probe_set = PROBE_SET.execute(&*client);
    let probe_get = PROBE_GET.query(&*client);
    let described = Statement::named(&describe_sql, &[]).execute(&*client);
    let set = probe_set.await;
    let in_txn = read_probe(probe_get).await;
    let stmt = described.await.map(|s| s.leak());
    let in_txn = match set.and(in_txn) {
        Ok(in_txn) => in_txn,
        Err(e) => {
            if let Ok(stmt) = stmt {
                close(&client, stmt);
            }
            return task.fail_setup(e.into());
        }
    };
    let stmt = match stmt {
        Ok(stmt) => stmt,
        Err(e) => return task.fail_setup(e.into()),
    };
    let names: Vec<String> = stmt
        .columns()
        .iter()
        .map(|c| quote_ident(c.name()))
        .collect();
    let codecs: Vec<ColumnCodec> = stmt
        .columns()
        .iter()
        .map(|c| ColumnCodec::new(c.r#type(), format!("column {:?}", c.name())))
        .collect();
    // Deallocated before the COPY starts: nothing else may be sent while it runs.
    close(&client, stmt);
    if codecs.is_empty() {
        return task.fail_setup(QueryError::Interface(format!(
            "{table} has no columns to copy into"
        )));
    }
    let copy_sql = format!(
        "COPY {table} ({}) FROM STDIN (FORMAT binary)",
        names.join(", ")
    );
    let _ = task.plan.set(Plan {
        kind: PlanKind::Copy,
        columns: codecs,
    });
    task.post(task.ready.clone(), Outcome::Done);

    // The COPY is sent as a simple Query, started by the first chunk. That chunk was fed
    // after Python saw `ready`, i.e. after the loop thread processed every completion posted
    // before it, so no statement held by one of them can be deallocated (a `Close` message)
    // in the middle of the COPY stream. With a simple-query COPY, data can follow the query
    // immediately: if the COPY fails to start, the server ignores CopyData/CopyDone and sends
    // exactly one ReadyForQuery, so xitca's response routing stays in step.
    let watch = shared.io.get().cloned();
    let mut stream: Option<RowSimpleStream> = None;
    let mut savepoint: Option<ExecFuture> = None;
    let mut release: Option<ExecFuture> = None;
    let (mut copy_started, mut stream_done) = (false, false);
    let (mut finished, mut aborted, mut failed) = (false, false, false);
    let (mut rows_sent, mut bytes_sent, mut base) = (0u64, 0u64, 0u64);
    loop {
        let input_open = !finished && !aborted;
        let streaming = stream.is_some() && !stream_done;
        if !input_open && !streaming {
            break;
        }
        enum Event {
            Server(Result<(), xitca_postgres::Error>),
            Input(Option<Msg>),
        }
        let event = tokio::select! {
            biased;
            r = next_copy_message(&mut stream), if streaming => Event::Server(r),
            m = task.rx.recv(), if input_open => Event::Input(m),
        };
        let mut error = None;
        match event {
            Event::Server(Err(e))
                if !copy_started && e.downcast_ref::<UnexpectedMessage>().is_some() =>
            {
                copy_started = true;
            }
            Event::Server(Err(e)) => {
                stream_done = true;
                error = Some(copy_error(e));
            }
            Event::Server(Ok(())) => stream_done = true,
            Event::Input(Some(Msg::Copy { data, rows, ack })) => {
                if !failed && !stream_done {
                    if stream.is_none() {
                        if in_txn {
                            savepoint = Some(SAVEPOINT.execute(&*client));
                        }
                        base = watch.as_ref().map_or(0, |w| w.written());
                        match copy_sql.as_str().query(&*client).await {
                            Ok(s) => stream = Some(s),
                            Err(e) => error = Some(e.into()),
                        }
                        if error.is_none() {
                            match send_message(&client, b'd', &[COPY_HEADER]) {
                                Ok(n) => bytes_sent += n,
                                Err(e) => error = Some(e.into()),
                            }
                        }
                    }
                    if error.is_none() {
                        match send_message(&client, b'd', &[&data]) {
                            Ok(n) => {
                                bytes_sent += n;
                                rows_sent += rows as u64;
                                wait_for_socket(watch.as_deref(), base, bytes_sent, &client).await;
                            }
                            Err(e) => error = Some(e.into()),
                        }
                    }
                }
                task.post(ack, Outcome::Done);
            }
            Event::Input(Some(Msg::Finish)) => {
                finished = true;
                if stream.is_some() && !stream_done && !failed {
                    let sent = send_message(&client, b'd', &[&(-1i16).to_be_bytes()])
                        .and_then(|_| send_message(&client, b'c', &[]));
                    match sent {
                        Ok(_) if in_txn => release = Some(RELEASE.execute(&*client)),
                        Ok(_) => {}
                        Err(e) => error = Some(e.into()),
                    }
                }
            }
            Event::Input(Some(msg @ Msg::Rows(..))) => {
                if let Some(ack) = msg.into_ack() {
                    task.post(ack, Outcome::Done);
                }
            }
            Event::Input(None) => {
                aborted = true;
                if stream.is_some() && !stream_done {
                    // The server answers with an error (57014) and ReadyForQuery.
                    let _ = send_message(&client, b'f', &[b"fastpg: copy_in() aborted\0"]);
                }
            }
        }
        if let Some(e) = error
            && !failed
            && !aborted
        {
            failed = true;
            task.post(task.done.clone(), Outcome::Failed(e));
        }
    }

    for fut in [savepoint, release].into_iter().flatten() {
        if let Err(e) = fut.await
            && !failed
            && !aborted
        {
            failed = true;
            task.post(task.done.clone(), Outcome::Failed(e.into()));
        }
    }
    if in_txn && stream.is_some() && (failed || aborted) {
        let _ = ROLLBACK_TO.execute(&*client).await;
    }
    task.drain_input();
    if !failed {
        let outcome = if aborted {
            Outcome::Done
        } else {
            Outcome::Affected(rows_sent)
        };
        task.post(task.done.clone(), outcome);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifiers_are_quoted() {
        assert_eq!(quote_ident("people"), "\"people\"");
        assert_eq!(quote_ident("we\"ird"), "\"we\"\"ird\"");
    }

    #[test]
    fn copy_header_layout() {
        assert_eq!(COPY_HEADER.len(), 11 + 4 + 4);
        assert!(COPY_HEADER.starts_with(b"PGCOPY\n\xff\r\n\0"));
    }
}
