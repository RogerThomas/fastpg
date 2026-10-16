//! Direct construction of `into=` rows: msgspec Structs, `NamedTuple`s, pydantic models and
//! dicts.
//!
//! `StructSpec` is a flattened description of a target type built once in Python from
//! `msgspec.inspect` (see `fastpg/_spec.py`). For each prepared statement we bind result
//! columns to fields once ([`Binding`]): matching by name, type-checking the PostgreSQL
//! column type against the field annotation, and verifying required fields are present.
//!
//! Per row, decoded cell values are passed straight to the Struct type via vectorcall, as
//! positional arguments when the columns line up with the field order, otherwise as keyword
//! arguments whose names are the Struct's own interned field-name objects (msgspec matches
//! those by identity first). No tuple, dict or other intermediate container is created.
//!
//! A `NamedTuple` gets one value per field in field order, with the field's default for a
//! field no column feeds. When calling the class would do no more than `tuple.__new__(cls,
//! values)` (checked in `fastpg/_spec.py`), the instance is allocated and filled here, as
//! CPython's `tuple_subtype_new` does; otherwise the class is called positionally. A dict row
//! (`StructSpec.for_dict()`) has no fields: every column is bound and keyed by its name,
//! interned once per statement.

use std::collections::{HashMap, HashSet};
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

/// How instances are built.
enum Mode {
    /// msgspec Struct: call the class.
    Struct,
    /// `NamedTuple`: one value per field, in field order.
    NamedTuple {
        /// The default for each field (`None` = required).
        defaults: Vec<Option<Py<PyAny>>>,
        /// Calling the class is equivalent to `tuple.__new__(cls, values)`: allocate and fill
        /// instances natively. Otherwise call the class positionally with every field.
        alloc: bool,
    },
    /// Pydantic model: build instances like `model_construct` instead of calling the class.
    Pydantic {
        /// The default for each field (`None` = no usable default).
        defaults: Vec<Option<Py<PyAny>>>,
        /// Offsets of `__pydantic_fields_set__`, `__pydantic_extra__`,
        /// `__pydantic_private__` (BaseModel's `__slots__`), when they are plain object slots.
        slots: Option<[pyo3::ffi::Py_ssize_t; 3]>,
    },
    /// A `dict` keyed by column name: every column is bound, whatever its type.
    Dict,
}

struct FieldSpec {
    attr: Py<PyString>,
    attr_name: String,
    /// The column name it is matched by (`rename=`, an alias), when not `attr_name`.
    column_name: Option<String>,
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

/// `(has_default, default)` pairs as passed from Python → the default of each field, if any.
fn unbind_defaults(defaults: Vec<(bool, Bound<'_, PyAny>)>) -> Vec<Option<Py<PyAny>>> {
    defaults
        .into_iter()
        .map(|(has, value)| has.then(|| value.unbind()))
        .collect()
}

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
    mode: Mode,
    /// A GC-tracked msgspec Struct without `__post_init__` or `__dict__`, or a `NamedTuple`
    /// without `__dict__` or a custom `__new__`: an instance holding only scalars can be left
    /// untracked, as msgspec itself does (see [`Binding::untrack`]).
    untrackable: bool,
}

impl StructSpec {
    /// The default of each field, for the modes that fill missing fields themselves.
    fn defaults(&self) -> Option<&[Option<Py<PyAny>>]> {
        match &self.mode {
            Mode::NamedTuple { defaults, .. } | Mode::Pydantic { defaults, .. } => Some(defaults),
            Mode::Struct | Mode::Dict => None,
        }
    }
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
    /// `named_tuple_defaults` (one `(has_default, default)` per field) switches to
    /// `NamedTuple` mode: one value per field, in field order, the default standing in for a
    /// field no column feeds. With `named_tuple_alloc` (calling the class only does
    /// `tuple.__new__(cls, values)`), instances are allocated and filled natively; otherwise
    /// the class is called positionally with the values.
    ///
    /// `untrackable`: the Struct type is GC-tracked (`gc=True`) and has neither
    /// `__post_init__` nor `__dict__` (a `NamedTuple`: neither `__dict__` nor a custom
    /// `__new__`), so instances built only from scalar columns may be left untracked.
    #[new]
    #[pyo3(signature = (
        struct_type,
        fields,
        *,
        forbid_unknown_fields,
        positional_fields,
        pydantic_defaults = None,
        named_tuple_defaults = None,
        named_tuple_alloc = false,
        untrackable = false,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        struct_type: Bound<'_, PyAny>,
        fields: Vec<FieldArg<'_>>,
        forbid_unknown_fields: bool,
        positional_fields: usize,
        pydantic_defaults: Option<Vec<(bool, Bound<'_, PyAny>)>>,
        named_tuple_defaults: Option<Vec<(bool, Bound<'_, PyAny>)>>,
        named_tuple_alloc: bool,
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
            let attr_name = attr.to_str()?.to_owned();
            specs.push(FieldSpec {
                column_name: (encode_name != attr_name).then(|| encode_name.clone()),
                attr_name,
                attr: attr.unbind(),
                kind,
                nullable,
                required,
                json_decoder: json_decoder.map(Bound::unbind),
            });
            by_name.insert(encode_name, i);
        }
        let mode = match (pydantic_defaults, named_tuple_defaults) {
            (Some(_), Some(_)) => {
                return Err(pyo3::exceptions::PyValueError::new_err(
                    "pydantic_defaults and named_tuple_defaults are mutually exclusive",
                ));
            }
            (Some(defaults), None) => {
                let off = |name| decode::member_offset(&struct_type, name);
                let slots = match (
                    off("__pydantic_fields_set__")?,
                    off("__pydantic_extra__")?,
                    off("__pydantic_private__")?,
                ) {
                    (Some(a), Some(b), Some(c)) => Some([a, b, c]),
                    _ => None,
                };
                Mode::Pydantic {
                    defaults: unbind_defaults(defaults),
                    slots,
                }
            }
            (None, Some(defaults)) => Mode::NamedTuple {
                defaults: unbind_defaults(defaults),
                alloc: named_tuple_alloc,
            },
            (None, None) => Mode::Struct,
        };
        Ok(Self {
            untrackable: untrackable && !matches!(mode, Mode::Pydantic { .. }),
            mode,
            cls: struct_type.unbind(),
            cls_name,
            fields: specs,
            by_name,
            forbid_unknown_fields,
            positional_fields,
        })
    }

    /// The spec for `into=dict`: one dict per row, keyed by column name.
    #[staticmethod]
    fn for_dict(py: Python<'_>) -> Self {
        Self {
            cls: py.get_type::<PyDict>().into_any().unbind(),
            cls_name: "dict".to_owned(),
            fields: Vec::new(),
            by_name: HashMap::new(),
            forbid_unknown_fields: false,
            positional_fields: 0,
            mode: Mode::Dict,
            untrackable: false,
        }
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
    /// A `str` field fed from json/jsonb: it gets the raw JSON text (ADR 0003).
    json_as_text: bool,
    nullable: bool,
    /// A pydantic `Decimal` field: pydantic validation refuses NaN and infinities (unless
    /// `allow_inf_nan=True`, which needs validation), so direct construction does too.
    finite_decimal: bool,
}

struct PydanticPlan {
    /// Every `__dict__` key of an instance, in field order: the defaults of fields with no
    /// column, `None` for those set from columns. Copied per row, then the column values
    /// replace the placeholders.
    template: Py<PyDict>,
    /// Names of the fields set from columns (copied into `__pydantic_fields_set__` per row).
    fields_set: Py<PyFrozenSet>,
}

/// One item of a `NamedTuple`.
enum Arg {
    /// The value of `Binding::columns[i]`.
    Column(usize),
    /// The field's default: no column feeds it.
    Default(Py<PyAny>),
}

/// How a row's decoded values become an instance.
enum Make {
    /// Call the class with the values: positionally (`None`: the columns are its leading
    /// fields, in order) or named by these keyword names.
    Call(Option<Py<PyTuple>>),
    /// Call the class positionally with one argument per field, in field order
    /// (`NamedTuple`s whose columns are not exactly their fields, in order).
    Fields(Vec<Arg>),
    /// Allocate a `NamedTuple` and fill its items, one per field in field order, as
    /// `tuple.__new__` does (`Mode::NamedTuple { alloc: true, .. }`).
    NamedTuple(Vec<Arg>),
    Pydantic(PydanticPlan),
    /// A dict keyed by column name: the interned names, one per column.
    Dict(Vec<Py<PyString>>),
}

/// How a statement's result columns map onto one target type.
pub struct Binding {
    columns: Vec<BoundColumn>,
    make: Make,
    /// Every field comes from a scalar column (no JSON, no defaults) of an `untrackable`
    /// Struct or `NamedTuple`, so no instance can hold a reference that could form a cycle.
    /// (A `NamedTuple` is immutable and, being `untrackable`, has no `__dict__`.)
    ///
    /// Dict rows are never untracked: CPython 3.14 tracks every dict and doesn't re-track
    /// an untracked one when a container is stored in it later, so a row given one
    /// (`row["k"] = row`) could form a cycle the GC would never see.
    ///
    /// msgspec untracks such instances itself only when every value's type lacks GC
    /// support; `uuid.UUID` and `decimal.Decimal` have it, so a row holding one stayed
    /// tracked although `decode` leaves those values untracked. Assigning a container to a
    /// field later re-tracks the instance (msgspec's `__setattr__`), as for msgspec's own
    /// untracked Structs.
    untrack: bool,
}

fn unsupported_column(py: Python<'_>, column: &Column) -> PyErr {
    py_err(
        py,
        "UnsupportedTypeError",
        format!(
            "column {:?} has PostgreSQL type {}, which fastpg cannot decode",
            column.name(),
            column.r#type().name()
        ),
    )
}

impl Binding {
    /// Every column, keyed by its name; a name appearing twice raises `TypeMismatchError`
    /// (one value would silently replace the other).
    fn build_dict(py: Python<'_>, columns: &[Column]) -> PyResult<Self> {
        let mut bound = Vec::with_capacity(columns.len());
        let mut keys = Vec::with_capacity(columns.len());
        let mut names = HashSet::with_capacity(columns.len());
        for (col, column) in columns.iter().enumerate() {
            if !names.insert(column.name()) {
                return Err(py_err(
                    py,
                    "TypeMismatchError",
                    format!(
                        "column {:?} appears more than once in the result; dict rows need \
                         distinct column names (rename one with AS)",
                        column.name()
                    ),
                ));
            }
            let Some(codec) = Codec::for_type(column.r#type()) else {
                return Err(unsupported_column(py, column));
            };
            keys.push(PyString::intern(py, column.name()).unbind());
            bound.push(BoundColumn {
                col,
                field: col,
                codec,
                int_as_float: false,
                json_as_text: false,
                nullable: true,
                finite_decimal: false,
            });
        }
        Ok(Self {
            columns: bound,
            make: Make::Dict(keys),
            untrack: false,
        })
    }

    /// The items of a `NamedTuple`: each field's column value, or its default.
    fn named_tuple_args(
        py: Python<'_>,
        spec: &StructSpec,
        bound: &[BoundColumn],
        defaults: &[Option<Py<PyAny>>],
    ) -> PyResult<Vec<Arg>> {
        let mut args = Vec::with_capacity(defaults.len());
        for (field, default) in defaults.iter().enumerate() {
            let arg = match (bound.iter().position(|b| b.field == field), default) {
                (Some(i), _) => Arg::Column(i),
                (None, Some(default)) => Arg::Default(default.clone_ref(py)),
                // Ruled out by the missing-field check in `build`.
                (None, None) => {
                    let name = spec.fields.get(field).map_or("?", |f| f.attr_name.as_str());
                    return Err(py_err(
                        py,
                        "MissingColumnError",
                        format!(
                            "{} requires field(s) {name} but the query does not return them",
                            spec.cls_name
                        ),
                    ));
                }
            };
            args.push(arg);
        }
        Ok(args)
    }

    pub fn build(py: Python<'_>, spec: &StructSpec, columns: &[Column]) -> PyResult<Self> {
        if matches!(spec.mode, Mode::Dict) {
            return Self::build_dict(py, columns);
        }
        let mut bound: Vec<BoundColumn> = Vec::with_capacity(columns.len());
        let mut seen = vec![false; spec.fields.len()];
        for (col, column) in columns.iter().enumerate() {
            let Some(&field) = spec.by_name.get(column.name()) else {
                if spec.forbid_unknown_fields {
                    // Name the setting the user wrote: pydantic's, or msgspec's.
                    let setting = if matches!(spec.mode, Mode::Pydantic { .. }) {
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
                return Err(unsupported_column(py, column));
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
                finite_decimal: f.kind == FieldKind::Decimal
                    && matches!(spec.mode, Mode::Pydantic { .. }),
            });
        }
        let missing: Vec<String> = spec
            .fields
            .iter()
            .enumerate()
            .zip(&seen)
            .filter(|((i, f), seen)| {
                // Pydantic and NamedTuple modes can only fill a missing field from a known
                // default.
                let no_default = spec
                    .defaults()
                    .is_some_and(|d| d.get(*i).is_none_or(Option::is_none));
                !**seen && (f.required || no_default)
            })
            // A renamed or aliased field names the column it needs.
            .map(|((_, f), _)| match &f.column_name {
                Some(column) => format!("{} (column {column:?})", f.attr_name),
                None => f.attr_name.clone(),
            })
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
        let in_field_order = bound.iter().enumerate().all(|(i, b)| b.field == i);
        let make = match &spec.mode {
            Mode::Pydantic { defaults, .. } => {
                let template = PyDict::new(py);
                for (field, (f, default)) in spec.fields.iter().zip(defaults).enumerate() {
                    if bound.iter().any(|b| b.field == field) {
                        template.set_item(f.attr.bind(py), py.None())?;
                    } else if let Some(default) = default {
                        template.set_item(f.attr.bind(py), default.bind(py))?;
                    }
                }
                let names = bound.iter().map(|b| spec.fields[b.field].attr.bind(py));
                Make::Pydantic(PydanticPlan {
                    template: template.unbind(),
                    fields_set: PyFrozenSet::new(py, names)?.unbind(),
                })
            }
            Mode::NamedTuple { defaults, alloc } => {
                if *alloc {
                    Make::NamedTuple(Self::named_tuple_args(py, spec, &bound, defaults)?)
                } else if in_field_order && bound.len() == spec.fields.len() {
                    Make::Call(None)
                } else {
                    Make::Fields(Self::named_tuple_args(py, spec, &bound, defaults)?)
                }
            }
            Mode::Struct | Mode::Dict => {
                if in_field_order && bound.len() <= spec.positional_fields {
                    Make::Call(None)
                } else {
                    let names = bound.iter().map(|b| spec.fields[b.field].attr.bind(py));
                    Make::Call(Some(PyTuple::new(py, names)?.unbind()))
                }
            }
        };
        let untrack = spec.untrackable
            && bound.len() == spec.fields.len()
            && bound
                .iter()
                .all(|b| !matches!(b.codec, Codec::Json | Codec::Jsonb));
        Ok(Self {
            columns: bound,
            make,
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
            // Dict rows have no fields: their columns decode to their natural Python types.
            let json_decoder = self
                .spec
                .fields
                .get(b.field)
                .and_then(|f| f.json_decoder.as_ref())
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
                Some(raw) if b.finite_decimal && decode::numeric_special(raw).is_some() => {
                    let f = &self.spec.fields[b.field];
                    let column = &self.columns[b.col];
                    return Err(py_err(
                        py,
                        "DecodeError",
                        format!(
                            "column {:?} ({}) is {}, but {}.{} is a pydantic Decimal, which must \
                             be finite (Field(allow_inf_nan=True) accepts NaN and infinities)",
                            column.name(),
                            column.r#type().name(),
                            decode::numeric_special(raw).unwrap_or("not finite"),
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
        let binding = self.binding;
        let obj = match &binding.make {
            Make::Pydantic(plan) => return self.construct_pydantic(plan),
            Make::Dict(keys) => return self.dict(keys),
            Make::Call(kwnames) => {
                let kwnames = kwnames.as_ref().map_or(ptr::null_mut(), Py::as_ptr);
                let args = self.values.iter().map(Bound::as_ptr);
                // SAFETY: `values` keeps every argument alive for the duration of the call and
                // `kwnames` (when set) has exactly one name per value.
                unsafe { call(py, &self.cls, &mut self.args, args, kwnames)? }
            }
            Make::Fields(fields) => {
                let values = &self.values;
                let args = fields.iter().map(|arg| match arg {
                    Arg::Column(i) => values[*i].as_ptr(),
                    Arg::Default(default) => default.as_ptr(),
                });
                // SAFETY: `values` keeps the column values alive for the duration of the call,
                // and the binding the defaults.
                unsafe { call(py, &self.cls, &mut self.args, args, ptr::null_mut())? }
            }
            Make::NamedTuple(items) => self.named_tuple(items)?,
        };
        if binding.untrack {
            decode::gc_untrack(&obj);
        }
        Ok(obj)
    }
}

/// Call `cls` with `args`: positionally, or all named by `kwnames` when it isn't null.
/// `spare` holds the arguments of calls wider than [`STACK_ARGS`].
///
/// # Safety
/// Every pointer `args` yields must be a live object for the duration of the call, and a
/// non-null `kwnames` must be a tuple of exactly one name per argument.
unsafe fn call<'py>(
    py: Python<'py>,
    cls: &Bound<'py, PyAny>,
    spare: &mut Vec<*mut pyo3::ffi::PyObject>,
    args: impl ExactSizeIterator<Item = *mut pyo3::ffi::PyObject>,
    kwnames: *mut pyo3::ffi::PyObject,
) -> PyResult<Bound<'py, PyAny>> {
    let n = args.len();
    let nargs = if kwnames.is_null() { n } else { 0 };
    // Slot 0 stays free for PY_VECTORCALL_ARGUMENTS_OFFSET.
    if n <= STACK_ARGS {
        let mut buf = [ptr::null_mut(); STACK_ARGS + 1];
        for (slot, arg) in buf[1..].iter_mut().zip(args) {
            *slot = arg;
        }
        // SAFETY: per this function's contract.
        return unsafe { decode::vectorcall(py, cls, &mut buf[..=n], nargs, kwnames) };
    }
    spare.clear();
    spare.push(ptr::null_mut());
    spare.extend(args);
    // SAFETY: per this function's contract.
    unsafe { decode::vectorcall(py, cls, spare, nargs, kwnames) }
}

impl<'py> StructBuilder<'_, 'py> {
    /// A dict row: each column's value under its interned name. (Copying a presized template
    /// and replacing its placeholders measured no faster for a 10-column row.)
    fn dict(&self, keys: &[Py<PyString>]) -> PyResult<Bound<'py, PyAny>> {
        let py = self.py;
        let dict = PyDict::new(py);
        for (key, value) in keys.iter().zip(&self.values) {
            dict.set_item(key.bind(py), value)?;
        }
        Ok(dict.into_any())
    }

    /// `tuple.__new__(cls, values)` for a `NamedTuple` class whose call does no more than that
    /// (checked in `fastpg/_spec.py`), as CPython's `tuple_subtype_new` does it: allocate an
    /// instance with one item per field, fill the items, reset the hash cache, make sure the
    /// instance is GC-tracked. Unlike the class's `__new__`, builds no intermediate tuple.
    fn named_tuple(&self, items: &[Arg]) -> PyResult<Bound<'py, PyAny>> {
        let py = self.py;
        // SAFETY: `cls` is a tuple subclass (a heap type, so GC-enabled, whose layout is a
        // tuple's: it has no `__dict__`, and tuple subclasses can't add slots), so tp_alloc
        // returns a zeroed instance with room for `items.len()` items and `ob_size` set to
        // that. Each item is set exactly once, with a new reference, before the instance
        // escapes, and nothing between the allocation and the last item runs Python code
        // (cloning only increments reference counts); a GC pass seeing the tracked instance
        // meanwhile skips its NULL items, as for any tuple under construction. The hash cache
        // is reset to -1 ("not computed") since the allocation zeroed it.
        unsafe {
            let tp = self.cls.as_ptr().cast::<pyo3::ffi::PyTypeObject>();
            let alloc = (*tp).tp_alloc.unwrap_or(pyo3::ffi::PyType_GenericAlloc);
            let n = items.len() as pyo3::ffi::Py_ssize_t;
            let obj = Bound::from_owned_ptr_or_err(py, alloc(tp, n))?;
            (*obj.as_ptr().cast::<pyo3::ffi::PyTupleObject>()).ob_hash = -1;
            for (i, item) in items.iter().enumerate() {
                let value = match item {
                    Arg::Column(c) => self.values[*c].clone().into_ptr(),
                    Arg::Default(default) => default.clone_ref(py).into_ptr(),
                };
                pyo3::ffi::PyTuple_SET_ITEM(obj.as_ptr(), i as pyo3::ffi::Py_ssize_t, value);
            }
            if pyo3::ffi::PyObject_GC_IsTracked(obj.as_ptr()) == 0 {
                pyo3::ffi::PyObject_GC_Track(obj.as_ptr().cast());
            }
            Ok(obj)
        }
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
            match self.spec.mode {
                Mode::Pydantic {
                    slots: Some([fields_set_at, extra_at, private_at]),
                    ..
                } => {
                    decode::init_slot(obj.as_ptr(), fields_set_at, fields_set.into_ptr());
                    decode::init_slot(obj.as_ptr(), extra_at, py.None().into_ptr());
                    decode::init_slot(obj.as_ptr(), private_at, py.None().into_ptr());
                }
                _ => {
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
