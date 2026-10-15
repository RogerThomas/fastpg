//! Direct `msgspec.Struct` construction.
//!
//! `StructSpec` is a flattened description of a Struct type built once in Python from
//! `msgspec.inspect` (see `fastpg/_spec.py`). For each prepared statement we bind result
//! columns to fields once ([`Binding`]): matching by name, type-checking the PostgreSQL
//! column type against the field annotation, and verifying required fields are present.
//!
//! Per row, decoded cell values are passed straight to the Struct type via vectorcall, as
//! positional arguments when the columns line up with the field order, otherwise as keyword
//! arguments whose names are the Struct's own interned field-name objects (msgspec matches
//! those by identity first). No tuple, dict or other intermediate container is created.

use std::collections::HashMap;
use std::ptr;

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PyString, PyTuple};
use xitca_postgres::Column;

use crate::decode::{self, Codec, DecodeFailure, PyCache, Target};
use crate::errors::py_err;
use crate::rows::RowBuffer;

/// Field kinds; must match `fastpg._spec.Kind`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum FieldKind {
    Any,
    Bool,
    Int,
    Float,
    Str,
    Bytes,
    DateTime,
    Date,
    Time,
    Uuid,
    Decimal,
    Json,
}

impl FieldKind {
    fn from_code(code: u8) -> Option<Self> {
        use FieldKind::*;
        [
            Any, Bool, Int, Float, Str, Bytes, DateTime, Date, Time, Uuid, Decimal, Json,
        ]
        .get(code as usize)
        .copied()
    }

    fn python_name(self) -> &'static str {
        match self {
            Self::Any => "Any",
            Self::Bool => "bool",
            Self::Int => "int",
            Self::Float => "float",
            Self::Str => "str",
            Self::Bytes => "bytes",
            Self::DateTime => "datetime",
            Self::Date => "date",
            Self::Time => "time",
            Self::Uuid => "UUID",
            Self::Decimal => "Decimal",
            Self::Json => "a JSON-decoded type",
        }
    }

    /// Whether a column with `codec` can populate a field of this kind, and if so whether
    /// integers must be widened to float.
    fn accepts(self, codec: Codec) -> Option<bool> {
        use Codec as C;
        let ok = match self {
            Self::Any => true,
            Self::Bool => codec == C::Bool,
            Self::Int => codec.is_int(),
            Self::Float => {
                if codec.is_int() {
                    return Some(true);
                }
                matches!(codec, C::Float4 | C::Float8)
            }
            Self::Str => matches!(codec, C::Text | C::Char | C::Json | C::Jsonb),
            Self::Bytes => codec == C::Bytea,
            Self::DateTime => matches!(codec, C::Timestamp | C::TimestampTz),
            Self::Date => codec == C::Date,
            Self::Time => codec == C::Time,
            Self::Uuid => codec == C::Uuid,
            Self::Decimal => codec == C::Numeric,
            Self::Json => matches!(codec, C::Json | C::Jsonb),
        };
        ok.then_some(false)
    }
}

struct FieldSpec {
    attr: Py<PyString>,
    attr_name: String,
    kind: FieldKind,
    nullable: bool,
    required: bool,
    json_decoder: Option<Py<PyAny>>,
}

/// `(attr_name, encode_name, kind, nullable, required, json_decoder)` as passed from Python.
type FieldArg<'py> = (
    Bound<'py, PyString>,
    String,
    u8,
    bool,
    bool,
    Option<Bound<'py, PyAny>>,
);

#[pyclass(frozen, module = "fastpg._native")]
pub struct StructSpec {
    cls: Py<PyAny>,
    cls_name: String,
    fields: Vec<FieldSpec>,
    /// encode name (what columns are matched against) → field index.
    by_name: HashMap<String, usize>,
    forbid_unknown_fields: bool,
    /// Leading fields that may be passed positionally (the rest are `kw_only`).
    positional_fields: usize,
    /// Set for pydantic models: build instances like `model_construct` instead of calling the
    /// class. Holds the default for each field (`None` = no usable default).
    pydantic_defaults: Option<Vec<Option<Py<PyAny>>>>,
    /// Offsets of `__pydantic_fields_set__`, `__pydantic_extra__`, `__pydantic_private__`
    /// (BaseModel's `__slots__`), when they are plain object slots.
    pydantic_slots: Option<[pyo3::ffi::Py_ssize_t; 3]>,
    /// A GC-tracked msgspec Struct without `__post_init__` or `__dict__`: an instance
    /// holding only scalars can be left untracked, as msgspec itself does (see
    /// [`Binding::untrack`]).
    untrackable: bool,
}

#[pymethods]
impl StructSpec {
    /// `fields`: `(attr_name, encode_name, kind, nullable, required, json_decoder)` tuples,
    /// in field order, where `attr_name` is the class's own (interned) field-name object and
    /// `json_decoder` is a callable taking JSON bytes.
    ///
    /// `pydantic_defaults` (one `(has_default, default)` per field) switches to pydantic
    /// mode: instances are built by filling `__dict__` and the `__pydantic_*__` slots
    /// directly, exactly as `BaseModel.model_construct` does, without calling `__init__`.
    ///
    /// `untrackable`: the Struct type is GC-tracked (`gc=True`) and has neither
    /// `__post_init__` nor `__dict__`, so instances built only from scalar columns may be
    /// left untracked.
    #[new]
    #[pyo3(signature = (
        struct_type,
        fields,
        *,
        forbid_unknown_fields,
        positional_fields,
        pydantic_defaults = None,
        untrackable = false,
    ))]
    fn new(
        struct_type: Bound<'_, PyAny>,
        fields: Vec<FieldArg<'_>>,
        forbid_unknown_fields: bool,
        positional_fields: usize,
        pydantic_defaults: Option<Vec<(bool, Bound<'_, PyAny>)>>,
        untrackable: bool,
    ) -> PyResult<Self> {
        let qualname = struct_type.getattr("__qualname__")?.extract::<String>()?;
        // `f.<locals>.Person` → `Person` for readable error messages.
        let cls_name = qualname
            .rsplit("<locals>.")
            .next()
            .unwrap_or(&qualname)
            .to_owned();
        let mut by_name = HashMap::with_capacity(fields.len());
        let mut specs = Vec::with_capacity(fields.len());
        for (i, (attr, encode_name, kind, nullable, required, json_decoder)) in
            fields.into_iter().enumerate()
        {
            let kind = FieldKind::from_code(kind).ok_or_else(|| {
                pyo3::exceptions::PyValueError::new_err(format!("unknown field kind {kind}"))
            })?;
            by_name.insert(encode_name, i);
            specs.push(FieldSpec {
                attr_name: attr.to_str()?.to_owned(),
                attr: attr.unbind(),
                kind,
                nullable,
                required,
                json_decoder: json_decoder.map(Bound::unbind),
            });
        }
        let pydantic_slots = match &pydantic_defaults {
            None => None,
            Some(_) => {
                let off = |name| decode::member_offset(&struct_type, name);
                match (
                    off("__pydantic_fields_set__")?,
                    off("__pydantic_extra__")?,
                    off("__pydantic_private__")?,
                ) {
                    (Some(a), Some(b), Some(c)) => Some([a, b, c]),
                    _ => None,
                }
            }
        };
        Ok(Self {
            pydantic_slots,
            untrackable: untrackable && pydantic_defaults.is_none(),
            cls: struct_type.unbind(),
            cls_name,
            fields: specs,
            by_name,
            forbid_unknown_fields,
            positional_fields,
            pydantic_defaults: pydantic_defaults.map(|d| {
                d.into_iter()
                    .map(|(has, value)| has.then(|| value.unbind()))
                    .collect()
            }),
        })
    }

    fn __repr__(&self) -> String {
        format!(
            "StructSpec({}, {} fields)",
            self.cls_name,
            self.fields.len()
        )
    }
}

struct BoundColumn {
    col: usize,
    field: usize,
    codec: Codec,
    int_as_float: bool,
    /// A `str` field fed from json/jsonb: it gets the raw JSON text (ADR 0016).
    json_as_text: bool,
    nullable: bool,
}

struct PydanticPlan {
    /// Every `__dict__` key of an instance, in field order: the defaults of fields with no
    /// column, `None` for those set from columns. Copied per row, then the column values
    /// replace the placeholders.
    template: Py<PyDict>,
    /// Names of the fields set from columns (copied into `__pydantic_fields_set__` per row).
    fields_set: Py<PyFrozenSet>,
}

/// How a statement's result columns map onto one Struct type.
pub struct Binding {
    columns: Vec<BoundColumn>,
    /// `None`: columns are the Struct's leading fields in order → positional call.
    kwnames: Option<Py<PyTuple>>,
    pydantic: Option<PydanticPlan>,
    /// Every field comes from a scalar column (no JSON, no defaults) of an `untrackable`
    /// Struct, so no instance can hold a reference that could form a cycle.
    ///
    /// msgspec untracks such instances itself only when every value's type lacks GC
    /// support; `uuid.UUID` and `decimal.Decimal` have it, so a row holding one stayed
    /// tracked although `decode` leaves those values untracked. Assigning a container to a
    /// field later re-tracks the instance (msgspec's `__setattr__`), as for msgspec's own
    /// untracked Structs.
    untrack: bool,
}

impl Binding {
    pub fn build(py: Python<'_>, spec: &StructSpec, columns: &[Column]) -> PyResult<Self> {
        let mut bound: Vec<BoundColumn> = Vec::with_capacity(columns.len());
        let mut seen = vec![false; spec.fields.len()];
        for (col, column) in columns.iter().enumerate() {
            let Some(&field) = spec.by_name.get(column.name()) else {
                if spec.forbid_unknown_fields {
                    // Name the setting the user wrote: pydantic's, or msgspec's.
                    let setting = if spec.pydantic_defaults.is_some() {
                        "extra=\"forbid\""
                    } else {
                        "forbid_unknown_fields=True"
                    };
                    return Err(py_err(
                        py,
                        "UnknownColumnError",
                        format!(
                            "column {:?} has no matching field on {} ({setting})",
                            column.name(),
                            spec.cls_name
                        ),
                    ));
                }
                continue;
            };
            let f = &spec.fields[field];
            if std::mem::replace(&mut seen[field], true) {
                return Err(py_err(
                    py,
                    "TypeMismatchError",
                    format!(
                        "column {:?} appears more than once in the result",
                        column.name()
                    ),
                ));
            }
            let ty = column.r#type();
            let Some(codec) = Codec::for_type(ty) else {
                return Err(py_err(
                    py,
                    "UnsupportedTypeError",
                    format!(
                        "column {:?} has PostgreSQL type {}, which fastpg cannot decode",
                        column.name(),
                        ty.name()
                    ),
                ));
            };
            let Some(int_as_float) = f.kind.accepts(codec) else {
                return Err(py_err(
                    py,
                    "TypeMismatchError",
                    format!(
                        "column {:?} has PostgreSQL type {}, but {}.{} expects {}",
                        column.name(),
                        ty.name(),
                        spec.cls_name,
                        f.attr_name,
                        f.kind.python_name()
                    ),
                ));
            };
            bound.push(BoundColumn {
                col,
                field,
                codec,
                int_as_float,
                json_as_text: f.kind == FieldKind::Str
                    && matches!(codec, Codec::Json | Codec::Jsonb),
                nullable: f.nullable,
            });
        }
        let missing: Vec<&str> = spec
            .fields
            .iter()
            .enumerate()
            .zip(&seen)
            .filter(|((i, f), seen)| {
                // Pydantic mode can only fill a missing field from a known default.
                let no_default = spec
                    .pydantic_defaults
                    .as_ref()
                    .is_some_and(|d| d[*i].is_none());
                !**seen && (f.required || no_default)
            })
            .map(|((_, f), _)| f.attr_name.as_str())
            .collect();
        if !missing.is_empty() {
            return Err(py_err(
                py,
                "MissingColumnError",
                format!(
                    "{} requires field(s) {} but the query does not return them",
                    spec.cls_name,
                    missing.join(", ")
                ),
            ));
        }
        let pydantic = match &spec.pydantic_defaults {
            None => None,
            Some(defaults) => {
                let template = PyDict::new(py);
                for (field, (f, default)) in spec.fields.iter().zip(defaults).enumerate() {
                    if bound.iter().any(|b| b.field == field) {
                        template.set_item(f.attr.bind(py), py.None())?;
                    } else if let Some(default) = default {
                        template.set_item(f.attr.bind(py), default.bind(py))?;
                    }
                }
                let names = bound.iter().map(|b| spec.fields[b.field].attr.bind(py));
                Some(PydanticPlan {
                    template: template.unbind(),
                    fields_set: PyFrozenSet::new(py, names)?.unbind(),
                })
            }
        };
        let positional = pydantic.is_none()
            && bound.len() <= spec.positional_fields
            && bound.iter().enumerate().all(|(i, b)| b.field == i);
        let kwnames = if positional || pydantic.is_some() {
            None
        } else {
            let names = bound.iter().map(|b| spec.fields[b.field].attr.bind(py));
            Some(PyTuple::new(py, names)?.unbind())
        };
        let untrack = spec.untrackable
            && bound.len() == spec.fields.len()
            && bound
                .iter()
                .all(|b| !matches!(b.codec, Codec::Json | Codec::Jsonb));
        Ok(Self {
            columns: bound,
            kwnames,
            pydantic,
            untrack,
        })
    }
}

fn column_error(py: Python<'_>, column: &Column, failure: DecodeFailure) -> PyErr {
    match failure {
        DecodeFailure::Py(e) => {
            let err = py_err(
                py,
                "DecodeError",
                format!(
                    "failed to decode column {:?} ({})",
                    column.name(),
                    column.r#type().name()
                ),
            );
            err.set_cause(py, Some(e));
            err
        }
        DecodeFailure::Invalid(msg) => py_err(
            py,
            "DecodeError",
            format!(
                "column {:?} ({}): {msg}",
                column.name(),
                column.r#type().name()
            ),
        ),
    }
}

/// Rows with at most this many columns pass their vectorcall arguments from the stack.
const STACK_ARGS: usize = 16;

/// Builds Struct instances row by row, reusing its argument buffers.
pub struct StructBuilder<'a, 'py> {
    py: Python<'py>,
    cache: &'a PyCache,
    spec: &'a StructSpec,
    binding: &'a Binding,
    columns: &'a [Column],
    cls: Bound<'py, PyAny>,
    values: Vec<Bound<'py, PyAny>>,
    /// Vectorcall arguments of rows wider than [`STACK_ARGS`].
    args: Vec<*mut pyo3::ffi::PyObject>,
}

impl<'a, 'py> StructBuilder<'a, 'py> {
    pub fn new(
        py: Python<'py>,
        cache: &'a PyCache,
        spec: &'a StructSpec,
        binding: &'a Binding,
        columns: &'a [Column],
    ) -> Self {
        Self {
            py,
            cache,
            spec,
            binding,
            columns,
            cls: spec.cls.bind(py).clone(),
            values: Vec::with_capacity(binding.columns.len()),
            args: Vec::new(),
        }
    }

    pub fn build(&mut self, rows: &RowBuffer, row: usize) -> PyResult<Bound<'py, PyAny>> {
        let py = self.py;
        self.values.clear();
        for b in &self.binding.columns {
            let json_decoder = self.spec.fields[b.field]
                .json_decoder
                .as_ref()
                .map(|d| d.bind(py));
            let value = match rows.cell(row, b.col) {
                None if b.nullable => py.None().into_bound(py),
                None => {
                    let f = &self.spec.fields[b.field];
                    return Err(py_err(
                        py,
                        "NullValueError",
                        format!(
                            "column {:?} is NULL but {}.{} is not optional",
                            self.columns[b.col].name(),
                            self.spec.cls_name,
                            f.attr_name
                        ),
                    ));
                }
                Some(raw) => {
                    let target = Target {
                        int_as_float: b.int_as_float,
                        json_as_text: b.json_as_text,
                        json_decoder,
                    };
                    decode::decode(py, self.cache, b.codec, raw, target)
                        .map_err(|e| column_error(py, &self.columns[b.col], e))?
                }
            };
            self.values.push(value);
        }
        if let Some(plan) = &self.binding.pydantic {
            return self.construct_pydantic(plan);
        }
        let obj = self.call()?;
        if self.binding.untrack {
            decode::gc_untrack(&obj);
        }
        Ok(obj)
    }
}

impl<'py> StructBuilder<'_, 'py> {
    /// Call the Struct class with `values` (positionally, or named by `kwnames`).
    fn call(&mut self) -> PyResult<Bound<'py, PyAny>> {
        let (nargs, kwnames) = match &self.binding.kwnames {
            None => (self.values.len(), ptr::null_mut()),
            Some(names) => (0, names.as_ptr()),
        };
        let n = self.values.len();
        // Slot 0 stays free for PY_VECTORCALL_ARGUMENTS_OFFSET.
        if n <= STACK_ARGS {
            let mut args = [ptr::null_mut(); STACK_ARGS + 1];
            for (slot, value) in args[1..].iter_mut().zip(&self.values) {
                *slot = value.as_ptr();
            }
            // SAFETY: `values` keeps every argument alive for the duration of the call and
            // `kwnames` (when set) has exactly one name per value.
            return unsafe {
                decode::vectorcall(self.py, &self.cls, &mut args[..=n], nargs, kwnames)
            };
        }
        self.args.clear();
        self.args.push(ptr::null_mut());
        self.args.extend(self.values.iter().map(Bound::as_ptr));
        // SAFETY: as above.
        unsafe { decode::vectorcall(self.py, &self.cls, &mut self.args, nargs, kwnames) }
    }

    /// `BaseModel.model_construct`, natively: allocate the instance without `__init__`, install
    /// a `__dict__` holding the field values (in field order) and set the three pydantic slots.
    /// Only used for models whose core schema proves validation would be a no-op (checked in
    /// `fastpg/_spec.py`); column types were checked against field types by the binding.
    fn construct_pydantic(&self, plan: &PydanticPlan) -> PyResult<Bound<'py, PyAny>> {
        let py = self.py;
        // The copy has every key, in field order, and is sized once: replacing the column
        // values neither inserts nor resizes.
        // SAFETY: PyDict_Copy of a dict returns a new reference to a dict (or NULL + error).
        let dict = unsafe {
            Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyDict_Copy(plan.template.as_ptr()))?
                .cast_into_unchecked::<PyDict>()
        };
        for (b, value) in self.binding.columns.iter().zip(&self.values) {
            dict.set_item(self.spec.fields[b.field].attr.bind(py), value)?;
        }
        // SAFETY: `cls` is a pydantic model class (a heap type with `__dict__` support);
        // tp_alloc returns a zeroed, GC-tracked instance. The dict is installed with
        // PyObject_GenericSetDict and the three slots are written at the offsets published by
        // BaseModel's member descriptors (falling back to descriptor-based setattr), which is
        // what `model_construct`'s `_object_setattr` does, minus the attribute lookups.
        unsafe {
            let tp = self.cls.as_ptr().cast::<pyo3::ffi::PyTypeObject>();
            let alloc = (*tp).tp_alloc.unwrap_or(pyo3::ffi::PyType_GenericAlloc);
            let obj = Bound::from_owned_ptr_or_err(py, alloc(tp, 0))?;
            if pyo3::ffi::PyObject_GenericSetDict(obj.as_ptr(), dict.as_ptr(), ptr::null_mut()) != 0
            {
                return Err(PyErr::fetch(py));
            }
            let fields_set =
                Bound::from_owned_ptr_or_err(py, pyo3::ffi::PySet_New(plan.fields_set.as_ptr()))?;
            match self.spec.pydantic_slots {
                Some([fields_set_at, extra_at, private_at]) => {
                    decode::init_slot(obj.as_ptr(), fields_set_at, fields_set.into_ptr());
                    decode::init_slot(obj.as_ptr(), extra_at, py.None().into_ptr());
                    decode::init_slot(obj.as_ptr(), private_at, py.None().into_ptr());
                }
                None => {
                    let none = py.None();
                    for (name, value) in [
                        (intern!(py, "__pydantic_fields_set__"), fields_set.as_ptr()),
                        (intern!(py, "__pydantic_extra__"), none.as_ptr()),
                        (intern!(py, "__pydantic_private__"), none.as_ptr()),
                    ] {
                        if pyo3::ffi::PyObject_GenericSetAttr(obj.as_ptr(), name.as_ptr(), value)
                            != 0
                        {
                            return Err(PyErr::fetch(py));
                        }
                    }
                }
            }
            Ok(obj)
        }
    }
}

/// Whether tuple rows of these columns hold only scalars (no json/jsonb values), so they
/// can be left untracked by the cyclic GC, as CPython itself does for tuples of atoms.
pub fn scalar_columns(codecs: &[Codec]) -> bool {
    codecs
        .iter()
        .all(|c| !matches!(c, Codec::Json | Codec::Jsonb))
}

/// Build one tuple per row using each column's natural Python type. `untrack`: the columns
/// are [`scalar_columns`].
pub fn tuple_row<'py>(
    py: Python<'py>,
    cache: &PyCache,
    codecs: &[Codec],
    untrack: bool,
    columns: &[Column],
    rows: &RowBuffer,
    row: usize,
) -> PyResult<Bound<'py, PyAny>> {
    // SAFETY: PyTuple_New(n) returns a tuple of n NULL slots; each is filled exactly once with
    // an owned reference before the tuple escapes. On early return the tuple is dropped, and
    // tuple deallocation tolerates NULL slots.
    unsafe {
        let tuple = Bound::from_owned_ptr_or_err(
            py,
            pyo3::ffi::PyTuple_New(codecs.len() as pyo3::ffi::Py_ssize_t),
        )?;
        for (col, codec) in codecs.iter().enumerate() {
            let value = match rows.cell(row, col) {
                None => py.None().into_bound(py),
                Some(raw) => decode::decode(py, cache, *codec, raw, Target::PLAIN)
                    .map_err(|e| column_error(py, &columns[col], e))?,
            };
            pyo3::ffi::PyTuple_SET_ITEM(
                tuple.as_ptr(),
                col as pyo3::ffi::Py_ssize_t,
                value.into_ptr(),
            );
        }
        if untrack {
            decode::gc_untrack(&tuple);
        }
        Ok(tuple)
    }
}

/// Collect `rows` into a list, building each element with `f`.
pub fn collect_list<'py>(
    py: Python<'py>,
    rows: &RowBuffer,
    mut f: impl FnMut(usize) -> PyResult<Bound<'py, PyAny>>,
) -> PyResult<Bound<'py, PyAny>> {
    let n = rows.nrows();
    // SAFETY: PyList_New(n) returns a list of n NULL slots; each slot is filled exactly once
    // with an owned reference. On early return the list is dropped, and list deallocation
    // tolerates NULL slots.
    unsafe {
        let list =
            Bound::from_owned_ptr_or_err(py, pyo3::ffi::PyList_New(n as pyo3::ffi::Py_ssize_t))?
                .cast_into_unchecked::<PyList>();
        for r in 0..n {
            let item = f(r)?;
            pyo3::ffi::PyList_SET_ITEM(list.as_ptr(), r as pyo3::ffi::Py_ssize_t, item.into_ptr());
        }
        Ok(list.into_any())
    }
}
