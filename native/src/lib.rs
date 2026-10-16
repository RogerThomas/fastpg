//! Native core of `fastpg`.
//!
//! Architecture (see README for the long version):
//!
//! * The asyncio event-loop thread runs everything. Python connects the database socket and
//!   registers it with `loop.add_reader` / `add_writer`; each readiness callback polls the
//!   connection's tasks (the `xitca-postgres` driver, the handshake, every query) on a small
//!   per-connection executor (`event_loop`). There is no tokio runtime and no extra thread.
//! * Query tasks copy the raw binary column values of each row into a flat arena
//!   (`rows::RowBuffer`) and queue a completion in the connection's mailbox. Once the woken
//!   tasks have run, the completions are decoded into Python objects (`decode`, `structs`,
//!   `msgpack`) and the awaiting asyncio futures resolved, in one go.
//! * A task woken from another thread (e.g. by a `__del__` there) wakes the loop through the
//!   mailbox's socketpair (`mailbox`), registered with `loop.add_reader`.

mod alloc_stats;
mod auth;
mod bulk;
mod connection;
mod decode;
mod errors;
mod event_loop;
mod json;
mod mailbox;
mod msgpack;
mod net;
mod params;
mod pgtime;
mod router;
mod rows;
mod stmt_cache;
mod structs;
mod tls;

use pyo3::prelude::*;

/// Number of Rust heap allocations made so far, or `None` when built without `alloc-stats`.
#[pyfunction]
fn rust_allocations() -> Option<u64> {
    alloc_stats::count()
}

/// The hosts of `dsn` as `(kind, host, port)` triples, for Python to connect the socket.
#[pyfunction]
fn dsn_targets(py: Python<'_>, dsn: &str) -> PyResult<Vec<(&'static str, String, u16)>> {
    net::targets(dsn).map_err(|e| e.into_pyerr(py))
}

/// libpq's `connect_timeout` of `dsn` in seconds (`None`: wait indefinitely); checks the DSN.
#[pyfunction]
fn dsn_connect_timeout(py: Python<'_>, dsn: &str) -> PyResult<Option<u32>> {
    net::connect_timeout(dsn).map_err(|e| e.into_pyerr(py))
}

/// Keeps the GIL on free-threaded Python (`gil_used = true`; PyO3's default is `false`): this
/// crate hasn't been audited for running without it, and a connection is bound to one event loop.
#[pymodule(gil_used = true)]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<connection::Connection>()?;
    m.add_class::<structs::StructSpec>()?;
    m.add_class::<bulk::RowReader>()?;
    m.add_class::<bulk::Bulk>()?;
    m.add_class::<router::Router>()?;
    m.add_function(wrap_pyfunction!(rust_allocations, m)?)?;
    m.add_function(wrap_pyfunction!(dsn_targets, m)?)?;
    m.add_function(wrap_pyfunction!(connection::after_fork_in_child, m)?)?;
    m.add_function(wrap_pyfunction!(dsn_connect_timeout, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
