//! The native connection object wrapped by `fastpg.Connection`.
//!
//! Every operation takes an asyncio future created by the Python wrapper and runs the
//! database round trip as a task of the connection's executor (`event_loop.rs`), polled on
//! the loop thread. Tasks queue [`Completion`]s in the mailbox; every entry point from Python
//! ends with [`Shared::pump`], which polls the woken tasks, turns completions into Python
//! objects and resolves the futures.
//!
//! Large `select_many` results are streamed: every [`CHUNK_ROWS`] rows the task queues the
//! rows received so far, so chunk N is decoded while chunk N+1 is still on the wire.

use std::future::IntoFuture;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};

use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyString, PyTuple};
use xitca_postgres::error::{DbError, SqlState};
use xitca_postgres::iter::AsyncLendingIterator;
use xitca_postgres::{Client, Driver, Execute, Statement};

use crate::bulk::{self, Bulk, RowReader};
use crate::decode;
use crate::errors::{QueryError, py_err};
use crate::event_loop;
use crate::json;
use crate::mailbox::Mailbox;
use crate::msgpack;
use crate::net::{self, IoWatch};
use crate::params::{self, Param, indexed};
use crate::rows::RowBuffer;
use crate::stmt_cache::{CachedStmt, StatementCache};
use crate::structs::{StructBuilder, StructSpec, collect_list, scalar_columns, tuple_row};

/// Rows per streamed chunk of a `select_many` result. 512 measured 5-8% less loop CPU than
/// 1,024 on 1,000-row fetches (the first chunk is decoded while the rest is in flight, and
/// the arena stays smaller) and the same on 10,000-row ones; smaller chunks cost more per
/// row, larger ones up to 18% more (`notes/perf-decode.md`).
const CHUNK_ROWS: usize = 512;

/// Most bytes of row buffers a connection keeps for reuse (see [`Shared::row_buffer`]): a
/// 512-row chunk of rows of a few hundred bytes, or hundreds of point-lookup rows.
const SPARE_ROW_BYTES: usize = 256 * 1024;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum Shape {
    /// Exactly one row (NoRowsError / TooManyRowsError otherwise).
    One,
    /// Zero or one row.
    OneOrNone,
    All,
}

impl Shape {
    fn from_code(code: u8) -> PyResult<Self> {
        match code {
            0 => Ok(Self::One),
            1 => Ok(Self::OneOrNone),
            2 => Ok(Self::All),
            _ => Err(pyo3::exceptions::PyValueError::new_err(format!(
                "invalid shape {code}"
            ))),
        }
    }
}

/// What the rows should become.
enum Target {
    None,
    Tuple,
    Struct(Py<StructSpec>),
    /// Rows encoded for a validating decoder. The names are the columns that feed `str`
    /// fields: a json/jsonb value there is encoded as its text (ADR 0016).
    Msgpack(Vec<String>),
    Json(Vec<String>),
}

#[derive(Clone, Copy)]
enum Op {
    Fetch(Shape),
    Execute,
}

/// The statement a query runs: from the cache, or prepared from its SQL on a cache miss.
enum StmtSource {
    Cached(Arc<CachedStmt>),
    Sql(String),
}

/// One awaiting Python call. Shared between the I/O task and the completions it posts; the
/// task hands its last reference to the final completion, so the Python objects inside are
/// always released on the loop thread.
pub(crate) struct Request {
    future: Py<PyAny>,
    target: Target,
    /// Rows decoded from earlier chunks of a streamed `select_many` (loop thread only).
    acc: Mutex<Option<Py<PyList>>>,
}

pub(crate) enum Outcome {
    Done,
    Affected(u64),
    /// A partial `select_many` result; more completions for this request follow.
    Chunk {
        stmt: Arc<CachedStmt>,
        rows: RowBuffer,
    },
    Rows {
        stmt: Arc<CachedStmt>,
        rows: RowBuffer,
        shape: Shape,
    },
    Failed(QueryError),
}

struct Completion {
    request: Arc<Request>,
    outcome: Outcome,
}

/// asyncio registrations of the database socket: `functools.partial`s made in Python.
struct Hooks {
    add_writer: Py<PyAny>,
    remove_writer: Py<PyAny>,
    /// Schedules `flush()` with `loop.call_soon`.
    flush_soon: Py<PyAny>,
    /// Unregisters the socket from the loop and closes it; called once the driver is gone.
    detach: Py<PyAny>,
    writer_on: bool,
}

pub(crate) struct Shared {
    mailbox: Mailbox<Completion>,
    /// Set by `close()`; a connect still in progress closes itself once established.
    closing: AtomicBool,
    client: Mutex<Option<Arc<Client>>>,
    cache: StatementCache,
    /// Ordinary operations hold it shared for their whole run; bulk writes (`bulk.rs`) hold it
    /// exclusively, so nothing else is pipelined into their transaction or COPY stream.
    pub(crate) gate: tokio::sync::RwLock<()>,
    /// Socket observer (EOF, bytes written), set once connected.
    pub(crate) io: std::sync::OnceLock<Arc<net::IoWatch>>,
    /// Set once the driver is gone. Every operation races against it: xitca 0.4 doesn't
    /// fail requests still waiting when its driver is dropped (their response senders live
    /// in state shared with the `Client`, which the waiting operations themselves keep
    /// alive), so without this they would wait forever.
    dead: tokio::sync::watch::Sender<bool>,
    /// Polls this connection's tasks on the loop thread (see `event_loop.rs`).
    exec: event_loop::Executor,
    /// The socket's readiness, fed by the loop's `add_reader` / `add_writer` callbacks.
    readiness: Arc<event_loop::IoState>,
    /// Set by `attach()`; cleared once the socket is detached.
    hooks: Mutex<Option<Hooks>>,
    /// Reused buffer for drained completions (only used on the loop thread).
    scratch: Mutex<Vec<Completion>>,
    /// A `flush()` is scheduled on the loop: new queries are queued for it instead of being
    /// written at once (see [`Shared::pump`]).
    flush_pending: AtomicBool,
    /// Row buffers of resolved results and the bytes they hold, for the next queries (loop
    /// thread only).
    spare_rows: Mutex<(Vec<RowBuffer>, usize)>,
}

impl Shared {
    /// Queue a completion for `pump`. Only tasks post, and they only run inside `pump` on
    /// the loop thread, which drains right after: no wake-up needed.
    pub(crate) fn post(&self, request: Arc<Request>, outcome: Outcome) {
        debug_assert!(self.exec.on_loop_thread());
        self.mailbox.push(Completion { request, outcome });
    }

    /// An empty buffer for a result's rows, reusing one a resolved result left behind: a
    /// point lookup then allocates nothing for its row (two allocations fewer), and each
    /// chunk of a large result starts at full size instead of regrowing from empty.
    fn row_buffer(&self, ncols: usize) -> RowBuffer {
        let mut guard = self
            .spare_rows
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        let (spare, held) = &mut *guard;
        match spare.pop() {
            Some(rows) => {
                *held -= rows.capacity_bytes();
                rows.reset(ncols)
            }
            None => RowBuffer::new(ncols),
        }
    }

    /// Keep a decoded result's buffer for [`Self::row_buffer`], within [`SPARE_ROW_BYTES`].
    fn keep_row_buffer(&self, rows: RowBuffer) {
        let size = rows.capacity_bytes();
        let mut guard = self
            .spare_rows
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        let (spare, held) = &mut *guard;
        if size > 0 && *held + size <= SPARE_ROW_BYTES {
            *held += size;
            spare.push(rows);
        }
    }

    /// Run `fut` as a task of this connection, polled on the loop thread. Tasks must not use
    /// anything that needs a tokio runtime (timers, `tokio::spawn`).
    pub(crate) fn spawn(&self, fut: impl Future<Output = ()> + Send + 'static) {
        self.exec.spawn(fut);
    }

    /// Poll the woken tasks, resolve every completed operation, then update the socket's
    /// loop registrations. Every Python entry point ends here.
    ///
    /// Write coalescing: resolving several results at once wakes several tasks, which the
    /// loop runs next and which typically send their next query right away. Each query
    /// would then be written with its own `send`. Instead, a `flush()` is scheduled behind
    /// those tasks' wake-ups, and queries submitted until it runs only queue their task
    /// (`flush_pending`): the flush writes them all at once. A lone result (sequential use)
    /// schedules nothing, so its next query is written at once as before.
    pub(crate) fn pump(&self, py: Python<'_>) {
        if !self.exec.on_loop_thread() {
            // E.g. a garbage-collector `__del__` on another thread: the task it woke poked
            // the mailbox, so the loop pumps instead.
            return;
        }
        // Resolving completions can wake tasks again: a completion's `Arc<CachedStmt>` may
        // hold the last `Arc<Client>`, whose drop queues `Terminate` and wakes the driver.
        let mut resolved = 0;
        loop {
            self.exec.run();
            resolved += self.drain(py);
            if !self.exec.has_ready() {
                break;
            }
        }
        let dead = *self.dead.borrow();
        if resolved > 1 && !dead {
            self.flush_soon(py);
        }
        self.sync_hooks(py, dead);
    }

    /// Schedule a `flush()` (once) for the queries the woken tasks are about to submit.
    fn flush_soon(&self, py: Python<'_>) {
        if self.flush_pending.swap(true, Ordering::AcqRel) {
            return;
        }
        let guard = self.hooks.lock().unwrap_or_else(PoisonError::into_inner);
        let scheduled = guard
            .as_ref()
            .is_some_and(|hooks| hooks.flush_soon.call0(py).is_ok());
        if !scheduled {
            // Nothing would run the flush (detached, or the loop is closed): write at once.
            self.flush_pending.store(false, Ordering::Release);
        }
    }

    /// Whether submissions should leave their task to the scheduled flush.
    fn defer_writes(&self) -> bool {
        self.flush_pending.load(Ordering::Acquire)
    }

    /// The scheduled flush: write every query queued since it was scheduled.
    fn flush(&self, py: Python<'_>) {
        self.flush_pending.store(false, Ordering::Release);
        self.pump(py);
    }

    /// Keep the loop's writer registration in step with what the socket needs; once the
    /// connection is dead, unregister and close the socket (Python owns the fd).
    fn sync_hooks(&self, py: Python<'_>, dead: bool) {
        let mut guard = self.hooks.lock().unwrap_or_else(PoisonError::into_inner);
        let Some(hooks) = guard.as_mut() else { return };
        if dead {
            if hooks.writer_on {
                let _ = hooks.remove_writer.call0(py);
            }
            let _ = hooks.detach.call0(py);
            *guard = None;
            return;
        }
        let want = self.readiness.wants_writer();
        if want != hooks.writer_on {
            let hook = if want {
                &hooks.add_writer
            } else {
                &hooks.remove_writer
            };
            let _ = hook.call0(py);
            hooks.writer_on = want;
        }
    }

    /// Resolve every queued completion; returns how many operations finished (streamed
    /// chunks don't count).
    fn drain(&self, py: Python<'_>) -> usize {
        let mut batch =
            std::mem::take(&mut *self.scratch.lock().unwrap_or_else(PoisonError::into_inner));
        self.mailbox.take_into(&mut batch);
        let finished = batch
            .iter()
            .filter(|c| !matches!(c.outcome, Outcome::Chunk { .. }))
            .count();
        for completion in batch.drain(..) {
            if let Some(rows) = resolve(py, completion) {
                self.keep_row_buffer(rows);
            }
        }
        *self.scratch.lock().unwrap_or_else(PoisonError::into_inner) = batch;
        finished
    }

    /// Run `op` unless (or until) the connection dies; then fail with `Closed`.
    pub(crate) async fn unless_dead<T>(
        &self,
        op: impl Future<Output = Result<T, QueryError>>,
    ) -> Result<T, QueryError> {
        let mut dead = self.dead.subscribe();
        tokio::select! {
            biased;
            result = op => result,
            _ = dead.wait_for(|dead| *dead) => Err(QueryError::Closed),
        }
    }

    fn client(&self) -> Option<Arc<Client>> {
        self.client
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    fn shutdown(&self) {
        let client = self
            .client
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        // Cached statements hold the client too; once they and any in-flight queries are
        // gone the client drops, which terminates the driver task. Statements still being
        // prepared are then not cached either.
        self.cache.close();
        drop(client);
    }
}

fn request(future: Py<PyAny>, target: Target) -> Arc<Request> {
    Arc::new(Request {
        future,
        target,
        acc: Mutex::new(None),
    })
}

/// A request whose future resolves to `None`, an int (`Outcome::Affected`) or an error.
pub(crate) fn plain_request(future: Py<PyAny>) -> Arc<Request> {
    request(future, Target::None)
}

/// From "connect attempt finished" to "driver gone": resolves `connected`, runs the driver
/// until the client is dropped or the server goes away, then marks the connection dead and
/// resolves `closed`.
async fn serve(
    shared: Arc<Shared>,
    connection: Result<(Client, Driver, Arc<IoWatch>), QueryError>,
    connected: Arc<Request>,
    closed: Arc<Request>,
) {
    match connection {
        Ok((client, driver, watch)) => {
            let _ = shared.io.set(watch.clone());
            *shared.client.lock().unwrap_or_else(PoisonError::into_inner) = Some(Arc::new(client));
            if shared.closing.load(Ordering::SeqCst) {
                // close() ran while we were connecting (e.g. connect() was cancelled).
                shared.shutdown();
            }
            shared.post(connected, Outcome::Done);
            // `biased` polls the driver first, so messages already received (e.g. the
            // server's final ErrorResponse) are routed before the driver is dropped on EOF;
            // `dead` (below) then fails every still-pending request.
            let driver = driver.into_future();
            tokio::pin!(driver);
            tokio::select! {
                biased;
                _ = &mut driver => {}
                () = watch.eof.notified() => {}
            }
            shared.shutdown();
        }
        Err(e) => shared.post(connected, Outcome::Failed(e)),
    }
    shared.dead.send_replace(true);
    shared.post(closed, Outcome::Done);
}

#[pyclass(frozen, module = "fastpg._native")]
pub struct Connection {
    shared: Arc<Shared>,
}

#[pymethods]
impl Connection {
    #[new]
    /// Create it on the event loop's thread: the connection's tasks only run there.
    #[pyo3(signature = (statement_cache_size = 256))]
    fn new(statement_cache_size: usize) -> PyResult<Self> {
        let mailbox = Mailbox::new()?;
        let exec = event_loop::Executor::new(mailbox.poker()?, std::thread::current().id());
        Ok(Self {
            shared: Arc::new(Shared {
                mailbox,
                closing: AtomicBool::new(false),
                client: Mutex::new(None),
                cache: StatementCache::new(statement_cache_size),
                gate: tokio::sync::RwLock::new(()),
                io: std::sync::OnceLock::new(),
                dead: tokio::sync::watch::Sender::new(false),
                exec,
                readiness: Arc::default(),
                hooks: Mutex::new(None),
                scratch: Mutex::new(Vec::new()),
                flush_pending: AtomicBool::new(false),
                spare_rows: Mutex::new((Vec::new(), 0)),
            }),
        })
    }

    /// Descriptor that becomes readable when a task was woken from another thread; the loop
    /// then calls `drain()`.
    fn fileno(&self) -> i32 {
        self.shared.mailbox.fileno()
    }

    #[getter]
    fn closed(&self) -> bool {
        self.shared.client().is_none()
    }

    /// Connect over `fd`, a connected non-blocking socket that Python keeps open: run TLS
    /// negotiation and the PostgreSQL handshake on this thread. `host` is the DSN host the
    /// socket connected to (the TLS server name). `add_writer` / `remove_writer` toggle the
    /// loop's writer registration; `flush_soon` schedules `flush()` (`loop.call_soon`);
    /// `detach` unregisters and closes the socket once the connection is over. Resolves
    /// `connected` once ready and `closed` when the connection ends.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (dsn, fd, *, unix, host, add_writer, remove_writer, flush_soon, detach, connected, closed))]
    fn attach(
        &self,
        py: Python<'_>,
        dsn: String,
        fd: i32,
        unix: bool,
        host: String,
        add_writer: Py<PyAny>,
        remove_writer: Py<PyAny>,
        flush_soon: Py<PyAny>,
        detach: Py<PyAny>,
        connected: Py<PyAny>,
        closed: Py<PyAny>,
    ) {
        *self
            .shared
            .hooks
            .lock()
            .unwrap_or_else(PoisonError::into_inner) = Some(Hooks {
            add_writer,
            remove_writer,
            flush_soon,
            detach,
            writer_on: false,
        });
        let socket = event_loop::LoopSocket::new(fd, unix, self.shared.readiness.clone());
        let shared = self.shared.clone();
        let connected = request(connected, Target::None);
        let closed = request(closed, Target::None);
        self.shared.spawn(async move {
            let host = (!unix).then_some(host.as_str());
            let connection = match net::config(&dsn) {
                Ok((cfg, tls)) => net::handshake(socket, cfg, &tls, host).await,
                Err(e) => Err(e),
            };
            serve(shared, connection, connected, closed).await;
        });
        self.shared.pump(py);
    }

    /// The socket is readable (`loop.add_reader` callback).
    fn on_readable(&self, py: Python<'_>) {
        self.shared.readiness.on_readable();
        self.shared.pump(py);
    }

    /// The socket is writable (`loop.add_writer` callback).
    fn on_writable(&self, py: Python<'_>) {
        self.shared.readiness.on_writable();
        self.shared.pump(py);
    }

    /// Write the queries queued since the flush was scheduled (`loop.call_soon` callback;
    /// see `Shared::pump`).
    fn flush(&self, py: Python<'_>) {
        self.shared.flush(py);
    }

    /// Stop accepting work. In-flight queries finish; then the connection is terminated.
    fn close(&self, py: Python<'_>) {
        self.shared.closing.store(true, Ordering::SeqCst);
        self.shared.shutdown();
        self.shared.pump(py);
    }

    #[pyo3(signature = (future, sql, params, shape, spec = None))]
    fn fetch(
        &self,
        future: Py<PyAny>,
        sql: &Bound<'_, PyString>,
        params: &Bound<'_, PyTuple>,
        shape: u8,
        spec: Option<Py<StructSpec>>,
    ) -> PyResult<()> {
        let target = spec.map_or(Target::Tuple, Target::Struct);
        self.submit(
            future,
            sql,
            params,
            Op::Fetch(Shape::from_code(shape)?),
            target,
        )
    }

    #[pyo3(signature = (future, sql, params, shape, text_columns = Vec::new()))]
    fn fetch_msgpack(
        &self,
        future: Py<PyAny>,
        sql: &Bound<'_, PyString>,
        params: &Bound<'_, PyTuple>,
        shape: u8,
        text_columns: Vec<String>,
    ) -> PyResult<()> {
        self.submit(
            future,
            sql,
            params,
            Op::Fetch(Shape::from_code(shape)?),
            Target::Msgpack(text_columns),
        )
    }

    #[pyo3(signature = (future, sql, params, shape, text_columns = Vec::new()))]
    fn fetch_json(
        &self,
        future: Py<PyAny>,
        sql: &Bound<'_, PyString>,
        params: &Bound<'_, PyTuple>,
        shape: u8,
        text_columns: Vec<String>,
    ) -> PyResult<()> {
        self.submit(
            future,
            sql,
            params,
            Op::Fetch(Shape::from_code(shape)?),
            Target::Json(text_columns),
        )
    }

    fn execute(
        &self,
        future: Py<PyAny>,
        sql: &Bound<'_, PyString>,
        params: &Bound<'_, PyTuple>,
    ) -> PyResult<()> {
        self.submit(future, sql, params, Op::Execute, Target::None)
    }

    /// Run one or more `;`-separated statements via the simple query protocol (no params).
    fn execute_script(&self, py: Python<'_>, future: Py<PyAny>, sql: String) -> PyResult<()> {
        let client = self.require_client(py)?;
        let shared = self.shared.clone();
        let req = request(future, Target::None);
        self.shared.spawn(async move {
            let _gate = shared.gate.read().await;
            let script = async { Ok(sql.as_str().execute(&*client).await?) };
            let outcome = match shared.unless_dead(script).await {
                Ok(_) => Outcome::Done,
                Err(e) => Outcome::Failed(e),
            };
            drop(client);
            shared.post(req, outcome);
        });
        self.shared.pump(py);
        Ok(())
    }

    /// Start `execute_many` (or `insert_many`, with a `RowReader` for its row type): one
    /// prepared statement, many parameter rows, pipelined. `ready` resolves once rows can be
    /// fed to the returned handle; `done` to the total affected row count. See `bulk.rs`.
    #[pyo3(signature = (ready, done, sql, reader = None))]
    fn execute_many(
        &self,
        py: Python<'_>,
        ready: Py<PyAny>,
        done: Py<PyAny>,
        sql: String,
        reader: Option<Py<RowReader>>,
    ) -> PyResult<Bulk> {
        let client = self.require_client(py)?;
        let statements = bulk::Statements::PerRow(sql);
        let bulk = bulk::start_execute_many(
            py,
            self.shared.clone(),
            client,
            statements,
            reader,
            ready,
            done,
        )?;
        self.shared.pump(py);
        Ok(bulk)
    }

    /// Start `insert_many`: like `execute_many`, but each statement is
    /// `<prefix> (<width> parameters), ...` covering `group` rows.
    #[allow(clippy::too_many_arguments)]
    fn insert_many(
        &self,
        py: Python<'_>,
        ready: Py<PyAny>,
        done: Py<PyAny>,
        prefix: String,
        width: usize,
        group: usize,
        reader: Py<RowReader>,
    ) -> PyResult<Bulk> {
        let client = self.require_client(py)?;
        let statements = bulk::Statements::Values {
            prefix,
            width,
            group: group.max(1),
        };
        let bulk = bulk::start_execute_many(
            py,
            self.shared.clone(),
            client,
            statements,
            Some(reader),
            ready,
            done,
        )?;
        self.shared.pump(py);
        Ok(bulk)
    }

    /// Start a binary `COPY <table> (<columns>) FROM STDIN`. `table` is already quoted;
    /// `columns` (unquoted) default to every column of `SELECT * FROM <table>`.
    #[pyo3(signature = (ready, done, table, reader, columns = None))]
    fn copy_in(
        &self,
        py: Python<'_>,
        ready: Py<PyAny>,
        done: Py<PyAny>,
        table: String,
        reader: Py<RowReader>,
        columns: Option<Vec<String>>,
    ) -> PyResult<Bulk> {
        let client = self.require_client(py)?;
        let bulk = bulk::start_copy_in(
            py,
            self.shared.clone(),
            client,
            table,
            columns,
            reader,
            ready,
            done,
        )?;
        self.shared.pump(py);
        Ok(bulk)
    }

    /// Run the tasks woken from another thread and resolve what completed: the loop's
    /// `add_reader` callback for `fileno()`.
    fn drain(&self, py: Python<'_>) {
        self.shared.pump(py);
    }

    fn statement_cache_len(&self) -> usize {
        self.shared.cache.len()
    }
}

impl Connection {
    fn require_client(&self, py: Python<'_>) -> PyResult<Arc<Client>> {
        self.shared
            .client()
            .ok_or_else(|| QueryError::Closed.into_pyerr(py))
    }

    fn submit(
        &self,
        future: Py<PyAny>,
        sql: &Bound<'_, PyString>,
        params: &Bound<'_, PyTuple>,
        op: Op,
        target: Target,
    ) -> PyResult<()> {
        let py = sql.py();
        let client = self.require_client(py)?;
        let params = params::from_py_tuple(params)?;
        let sql = sql.to_str()?;
        let source = match self.shared.cache.get(sql) {
            Some(stmt) => StmtSource::Cached(stmt),
            None => StmtSource::Sql(sql.to_owned()),
        };
        let shared = self.shared.clone();
        let stream = matches!(target, Target::Tuple | Target::Struct(_));
        let req = request(future, target);
        self.shared.spawn(async move {
            // Released after posting: see `bulk::copy_in_task` on why that order matters.
            let _gate = shared.gate.read().await;
            let outcome = shared
                .unless_dead(run(&shared, &req, stream, client, source, &params, op))
                .await
                .unwrap_or_else(Outcome::Failed);
            shared.post(req, outcome);
        });
        if !self.shared.defer_writes() {
            self.shared.pump(py);
        }
        Ok(())
    }
}

/// A cached statement the server no longer accepts: deallocated behind our back
/// (`DEALLOCATE ALL`, `DISCARD ALL`) or invalidated by DDL changing its result type.
/// Both errors are raised at Bind, before anything executes, so re-preparing is safe.
fn is_stale_statement(e: &xitca_postgres::Error) -> bool {
    e.downcast_ref::<DbError>().is_some_and(|db| {
        *db.code() == SqlState::INVALID_SQL_STATEMENT_NAME
            || (*db.code() == SqlState::FEATURE_NOT_SUPPORTED
                && db
                    .message()
                    .contains("cached plan must not change result type"))
    })
}

async fn prepare(
    shared: &Shared,
    client: &Arc<Client>,
    sql: &str,
) -> Result<Arc<CachedStmt>, QueryError> {
    let prepared = Statement::named(sql, &[]).execute(&**client).await?.leak();
    Ok(shared.cache.insert(
        sql.to_owned(),
        Arc::new(CachedStmt::new(sql, prepared, client.clone())),
    ))
}

async fn run(
    shared: &Shared,
    req: &Arc<Request>,
    stream_chunks: bool,
    client: Arc<Client>,
    source: StmtSource,
    params: &[Param],
    op: Op,
) -> Result<Outcome, QueryError> {
    let mut stmt = match source {
        StmtSource::Cached(stmt) => stmt,
        StmtSource::Sql(sql) => prepare(shared, &client, &sql).await?,
    };
    let mut retried = false;
    loop {
        let mut delivered = false;
        match run_once(
            shared,
            req,
            stream_chunks,
            &client,
            &stmt,
            params,
            op,
            &mut delivered,
        )
        .await
        {
            Err(QueryError::Pg(e)) if !retried && !delivered && is_stale_statement(&e) => {
                retried = true;
                shared.cache.evict(&stmt);
                stmt = prepare(shared, &client, stmt.sql()).await?;
            }
            outcome => return outcome,
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn run_once(
    shared: &Shared,
    req: &Arc<Request>,
    stream_chunks: bool,
    client: &Arc<Client>,
    stmt: &Arc<CachedStmt>,
    params: &[Param],
    op: Op,
    delivered: &mut bool,
) -> Result<Outcome, QueryError> {
    let statement = stmt.statement();
    if params.len() != statement.params().len() {
        return Err(QueryError::Parameter(format!(
            "query expects {} parameter(s), got {}",
            statement.params().len(),
            params.len()
        )));
    }
    match op {
        Op::Execute => {
            let affected = statement.bind(indexed(params)).execute(&**client).await?;
            Ok(Outcome::Affected(affected))
        }
        Op::Fetch(shape) => {
            let ncols = statement.columns().len();
            let mut stream = statement.bind(indexed(params)).query(&**client).await?;
            let mut rows = shared.row_buffer(ncols);
            let mut seen = 0usize;
            // Always drain the stream: the protocol requires reading to ReadyForQuery.
            while let Some(row) = stream.try_next().await? {
                seen += 1;
                if shape == Shape::All || seen == 1 {
                    rows.push_row(&row)?;
                }
                if stream_chunks && shape == Shape::All && rows.nrows() == CHUNK_ROWS {
                    let chunk = std::mem::replace(&mut rows, shared.row_buffer(ncols));
                    *delivered = true;
                    shared.post(
                        req.clone(),
                        Outcome::Chunk {
                            stmt: stmt.clone(),
                            rows: chunk,
                        },
                    );
                }
            }
            match shape {
                Shape::One if seen == 0 => Err(QueryError::NoRows),
                Shape::One | Shape::OneOrNone if seen > 1 => Err(QueryError::TooManyRows),
                _ => Ok(Outcome::Rows {
                    stmt: stmt.clone(),
                    rows,
                    shape,
                }),
            }
        }
    }
}

/// Resolve one completion; returns the row buffer it is done with, for reuse.
fn resolve(py: Python<'_>, completion: Completion) -> Option<RowBuffer> {
    let Completion { request, outcome } = completion;
    let future = request.future.bind(py);
    let (result, rows) = match outcome {
        Outcome::Chunk { stmt, rows } => {
            // A cancelled awaiter (or a chunk that already failed) leaves a done future
            // behind: don't decode the rest of a large result for nobody.
            match future
                .call_method0(intern!(py, "done"))
                .and_then(|d| d.is_truthy())
            {
                Ok(false) => {}
                _ => return Some(rows),
            }
            match decode_chunk(py, &request, &stmt, &rows) {
                Ok(()) => return Some(rows),
                Err(e) => (Err(e), Some(rows)),
            }
        }
        Outcome::Rows { stmt, rows, shape } => (
            materialize_rows(py, &request, &stmt, &rows, shape),
            Some(rows),
        ),
        outcome => (materialize(py, &request, outcome), None),
    };
    // On a done future (a cancelled awaiter) these raise InvalidStateError, which is
    // ignored: nobody waits for the result. Checking `done()` first would cost a Python
    // call for every query instead of an exception for the rare cancelled one.
    let _ = match result {
        Ok(value) => future.call_method1(intern!(py, "set_result"), (value,)),
        Err(err) => future.call_method1(intern!(py, "set_exception"), (err.into_value(py),)),
    };
    rows
}

/// Decode a streamed chunk into the request's accumulator list.
fn decode_chunk(
    py: Python<'_>,
    request: &Request,
    stmt: &CachedStmt,
    rows: &RowBuffer,
) -> PyResult<()> {
    let decoded = decode_rows(py, &request.target, stmt, rows)?;
    let mut acc = request.acc.lock().unwrap_or_else(PoisonError::into_inner);
    match &*acc {
        Some(list) => {
            // list += decoded
            list.bind(py)
                .call_method1(intern!(py, "extend"), (decoded,))?;
        }
        None => *acc = Some(decoded.cast_into::<PyList>()?.unbind()),
    }
    Ok(())
}

/// Builds one Python object (tuple or Struct) per buffered row.
enum RowMaker<'a, 'py> {
    Struct(StructBuilder<'a, 'py>),
    Tuple {
        codecs: Arc<[decode::Codec]>,
        /// No json/jsonb column: rows can be left untracked by the cyclic GC.
        untrack: bool,
        cache: &'a decode::PyCache,
        columns: &'a [xitca_postgres::Column],
    },
}

impl<'a, 'py: 'a> RowMaker<'a, 'py> {
    fn new(
        py: Python<'py>,
        target: &'a Target,
        stmt: &'a CachedStmt,
        binding: &'a mut Option<Arc<crate::structs::Binding>>,
    ) -> PyResult<Self> {
        let columns = stmt.statement().columns();
        let cache = decode::cache(py)?;
        Ok(match target {
            Target::Struct(spec) => {
                let spec = spec.bind(py);
                // Bind (and type-check) even for empty results so mismatches surface early.
                let b = binding.insert(stmt.binding(py, spec)?);
                Self::Struct(StructBuilder::new(py, cache, spec.get(), b, columns))
            }
            _ => {
                let codecs = stmt
                    .codecs()
                    .map_err(|m| py_err(py, "UnsupportedTypeError", m))?;
                Self::Tuple {
                    untrack: scalar_columns(&codecs),
                    codecs,
                    cache,
                    columns,
                }
            }
        })
    }

    fn make(&mut self, py: Python<'py>, rows: &RowBuffer, r: usize) -> PyResult<Bound<'py, PyAny>> {
        match self {
            Self::Struct(builder) => builder.build(rows, r),
            Self::Tuple {
                codecs,
                untrack,
                cache,
                columns,
            } => tuple_row(py, cache, codecs, *untrack, columns, rows, r),
        }
    }
}

/// Every row of `rows` as a list of tuples or Structs.
fn decode_rows<'py>(
    py: Python<'py>,
    target: &Target,
    stmt: &CachedStmt,
    rows: &RowBuffer,
) -> PyResult<Bound<'py, PyAny>> {
    let mut binding = None;
    let mut maker = RowMaker::new(py, target, stmt, &mut binding)?;
    collect_list(py, rows, |r| maker.make(py, rows, r))
}

fn materialize<'py>(
    py: Python<'py>,
    request: &Request,
    outcome: Outcome,
) -> PyResult<Bound<'py, PyAny>> {
    match outcome {
        Outcome::Done | Outcome::Chunk { .. } => Ok(py.None().into_bound(py)),
        Outcome::Affected(n) => Ok(n.into_pyobject(py)?.into_any()),
        Outcome::Failed(e) => Err(e.into_pyerr(py)),
        Outcome::Rows { stmt, rows, shape } => materialize_rows(py, request, &stmt, &rows, shape),
    }
}

fn materialize_rows<'py>(
    py: Python<'py>,
    request: &Request,
    stmt: &CachedStmt,
    rows: &RowBuffer,
    shape: Shape,
) -> PyResult<Bound<'py, PyAny>> {
    match &request.target {
        Target::None => Ok(py.None().into_bound(py)),
        target @ (Target::Msgpack(text_columns) | Target::Json(text_columns)) => {
            let single = shape != Shape::All;
            if single && rows.nrows() == 0 {
                return Ok(py.None().into_bound(py));
            }
            let codecs = stmt
                .codecs()
                .map_err(|m| py_err(py, "UnsupportedTypeError", m))?;
            let names: Vec<&str> = stmt
                .statement()
                .columns()
                .iter()
                .map(|c| c.name())
                .collect();
            let as_text: Vec<bool> = names
                .iter()
                .zip(codecs.iter())
                .map(|(name, codec)| {
                    matches!(codec, decode::Codec::Json | decode::Codec::Jsonb)
                        && text_columns.iter().any(|t| t == name)
                })
                .collect();
            let encoded = match target {
                Target::Json(_) => json::encode_rows(rows, &names, &codecs, &as_text, single),
                _ => msgpack::encode_rows(rows, &names, &codecs, &as_text, single),
            }
            .map_err(|m| py_err(py, "DecodeError", m))?;
            Ok(PyBytes::new(py, &encoded).into_any())
        }
        target @ (Target::Tuple | Target::Struct(_)) => {
            if shape != Shape::All {
                // Single-row shapes: at most one row was buffered.
                let mut binding = None;
                let mut maker = RowMaker::new(py, target, stmt, &mut binding)?;
                return match rows.nrows() {
                    0 => Ok(py.None().into_bound(py)),
                    _ => maker.make(py, rows, 0),
                };
            }
            let decoded = decode_rows(py, target, stmt, rows)?;
            let acc = request
                .acc
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .take();
            match acc {
                None => Ok(decoded),
                Some(list) => {
                    let list = list.into_bound(py);
                    list.call_method1(intern!(py, "extend"), (decoded,))?;
                    Ok(list.into_any())
                }
            }
        }
    }
}
