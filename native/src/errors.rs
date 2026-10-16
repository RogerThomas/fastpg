//! Error plumbing. Exception classes live in `fastpg/errors.py` (typed, documented,
//! subclassable); Rust only instantiates them.

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyModule;
use xitca_postgres::error::{ClosedByDriver, DbError, DriverDown, InvalidParamCount};

use crate::params::ParamError;

static ERRORS: PyOnceLock<Py<PyModule>> = PyOnceLock::new();

fn module(py: Python<'_>) -> PyResult<&Bound<'_, PyModule>> {
    ERRORS
        .get_or_try_init(py, || Ok(py.import("fastpg.errors")?.unbind()))
        .map(|m| m.bind(py))
}

/// Instantiate `fastpg.errors.<name>(message)`.
pub fn py_err(py: Python<'_>, name: &str, message: impl Into<String>) -> PyErr {
    let build = || -> PyResult<PyErr> {
        let cls = module(py)?.getattr(name)?;
        Ok(PyErr::from_value(cls.call1((message.into(),))?))
    };
    build().unwrap_or_else(|e| e)
}

/// Failures produced by the I/O tasks (no Python objects inside: tasks must be `Send`).
pub enum QueryError {
    Pg(xitca_postgres::Error),
    NoRows,
    TooManyRows,
    /// Closed by `close()`.
    Closed,
    /// Ended by the server or the network, not by `close()`.
    Lost,
    Parameter(String),
    Interface(String),
    /// A bulk write failed at this (0-based) row of the caller's input.
    AtRow(usize, Box<QueryError>),
    /// A multi-row statement covering rows `first..first + count` failed (which row is
    /// unknown: the server reports errors per statement).
    InRows(usize, usize, Box<QueryError>),
}

impl From<xitca_postgres::Error> for QueryError {
    fn from(e: xitca_postgres::Error) -> Self {
        Self::Pg(e)
    }
}

impl QueryError {
    pub fn into_pyerr(self, py: Python<'_>) -> PyErr {
        match self {
            Self::NoRows => py_err(py, "NoRowsError", "query returned no rows"),
            Self::TooManyRows => py_err(
                py,
                "TooManyRowsError",
                "query returned more than one row; use select_many() or add LIMIT 1",
            ),
            Self::Closed => py_err(py, "ConnectionClosedError", "connection is closed"),
            Self::Lost => py_err(
                py,
                "ConnectionClosedError",
                "connection lost: closed by the server or the network",
            ),
            Self::Parameter(msg) => py_err(py, "ParameterError", msg),
            Self::Interface(msg) => py_err(py, "InterfaceError", msg),
            Self::Pg(e) => pg_error(py, &e),
            Self::AtRow(index, e) => {
                let err = e.into_pyerr(py);
                let note = format!("raised by row {index} (0-based) of the bulk write");
                with_row_index(py, err, index, Some(&note))
            }
            Self::InRows(first, count, e) => {
                let err = e.into_pyerr(py);
                let note = format!(
                    "raised by the multi-row INSERT of rows {first}..{} (0-based); use \
                     execute_many() for the exact row",
                    first + count - 1
                );
                let _ = err.value(py).call_method1("add_note", (note,));
                err
            }
        }
    }
}

/// Set `row_index` on a fastpg exception (and optionally attach a PEP 678 note).
pub fn with_row_index(py: Python<'_>, err: PyErr, index: usize, note: Option<&str>) -> PyErr {
    let value = err.value(py);
    // Best effort: the exception is still raised if decorating it fails.
    let _ = value.setattr("row_index", index);
    if let Some(note) = note {
        let _ = value.call_method1("add_note", (note,));
    }
    err
}

fn pg_error(py: Python<'_>, e: &xitca_postgres::Error) -> PyErr {
    if let Some(db) = e.downcast_ref::<DbError>() {
        return db_error(py, db).unwrap_or_else(|err| err);
    }
    if let Some(p) = e.downcast_ref::<ParamError>() {
        return py_err(py, "ParameterError", p.to_string());
    }
    if let Some(p) = e.downcast_ref::<InvalidParamCount>() {
        return py_err(py, "ParameterError", p.to_string());
    }
    if e.is_driver_down()
        || e.downcast_ref::<DriverDown>().is_some()
        || e.downcast_ref::<ClosedByDriver>().is_some()
    {
        return py_err(py, "ConnectionClosedError", format!("connection lost: {e}"));
    }
    py_err(py, "InterfaceError", e.to_string())
}

/// Server error → `fastpg.errors.DatabaseError` subclass chosen by SQLSTATE (in Python).
fn db_error(py: Python<'_>, db: &DbError) -> PyResult<PyErr> {
    let position = match db.position() {
        Some(xitca_postgres::error::ErrorPosition::Original(p)) => Some(*p),
        _ => None,
    };
    let exc = module(py)?
        .getattr("DatabaseError")?
        .getattr("from_server")?
        .call1((
            db.code().code(),
            db.message(),
            db.severity(),
            db.detail(),
            db.hint(),
            position,
            db.schema(),
            db.table(),
            db.column(),
            db.datatype(),
            db.constraint(),
        ))?;
    Ok(PyErr::from_value(exc))
}
