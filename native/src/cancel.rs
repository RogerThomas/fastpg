//! Server-side cancel: a statement whose caller gave up (a cancelled task, `asyncio.timeout`)
//! is cancelled on the server too, when it is the only operation in flight on its connection.
//!
//! PostgreSQL cancels with a CancelRequest (length 16, code 80877102, the backend's process id
//! and secret key from its `BackendKeyData`, which `net::Framer` keeps) sent on a new
//! connection to the same server, which closes that connection once it has signalled the
//! backend. The backend cancels whatever it is running, so with several callers' statements
//! pipelined on the connection it could cancel someone else's: then nothing is sent, and the
//! abandoned statement runs to its end as before.
//!
//! The race: a cancel that arrives after its statement finished hits the next one. The server
//! signals the backend before it closes the cancel connection, and an idle backend ignores the
//! signal; so from the moment a cancel begins until its socket has closed (or it failed before
//! its request was written), nothing is sent on the connection: every operation waits for
//! [`Cancels::settled`] before it sends anything, as asyncpg's `_wait_for_cancellation` does.
//!
//! Python decides and connects (`fastpg._connection._Canceller`): [`begin`] says whether to
//! cancel; Python connects a socket to the address the connection reached, bounded by
//! `connect_timeout`, and hands it to [`Cancel::send`], whose task (polled on the loop thread
//! like the connection's own) negotiates TLS when the connection uses TLS (as libpq 17 does),
//! writes the request, and waits for the server to close the socket.

use std::io;
use std::sync::atomic::{AtomicBool, AtomicU8, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock, PoisonError};

use bytes::BytesMut;
use postgres_protocol::message::frontend;
use pyo3::prelude::*;
use tokio::sync::Notify;
use xitca_io::io::{AsyncIo, Interest};
use xitca_postgres::Config;

use crate::connection::{Outcome, Request, Shared, plain_request};
use crate::errors::QueryError;
use crate::event_loop::{IoState, LoopSocket};
use crate::tls::{self, TlsOptions};

/// A connection's cancel state (part of `connection::Shared`).
///
/// [`begin`] needs to know whether the abandoned operation is the only one on the wire (past
/// its gates and not finished): the operations there are counted, and the addresses of their
/// result futures summed, so that with one operation the sum is its future. Two relaxed
/// atomic additions per operation, and no list.
#[derive(Default)]
pub struct Cancels {
    count: AtomicUsize,
    sum: AtomicUsize,
    /// A cancel is in progress: from [`begin`] until it ended (see [`Run::end`]).
    active: AtomicBool,
    /// Notified when `active` is cleared.
    settled: Notify,
    /// The socket of the cancel in progress, once Python has connected it.
    io: Mutex<Option<CancelIo>>,
    /// The TLS settings (TLS required) and server name of a connection that uses TLS: its
    /// cancel requests use TLS too.
    tls: OnceLock<(TlsOptions, String)>,
}

/// An operation on the wire (see [`Cancels`]), counted out when dropped.
pub struct InFlight<'a> {
    cancels: &'a Cancels,
    key: usize,
}

impl Drop for InFlight<'_> {
    fn drop(&mut self) {
        self.cancels.count.fetch_sub(1, Ordering::Relaxed);
        self.cancels.sum.fetch_sub(self.key, Ordering::Relaxed);
    }
}

impl Cancels {
    /// Count an operation as on the wire until the guard drops; `future` is its result future.
    pub fn track(&self, future: &Py<PyAny>) -> InFlight<'_> {
        let key = future.as_ptr() as usize;
        self.count.fetch_add(1, Ordering::Relaxed);
        // Wraps around (as does the subtraction): only the sum of one key is ever compared.
        self.sum.fetch_add(key, Ordering::Relaxed);
        InFlight { cancels: self, key }
    }

    fn active(&self) -> bool {
        self.active.load(Ordering::Acquire)
    }

    /// Wait until no cancel is in progress: call it before sending anything.
    pub async fn settled(&self) {
        while self.active() {
            let settled = self.settled.notified();
            tokio::pin!(settled);
            settled.as_mut().enable();
            if !self.active() {
                return;
            }
            settled.await;
        }
    }

    fn settle(&self) {
        self.active.store(false, Ordering::Release);
        self.settled.notify_waiters();
    }

    /// Whether the operation whose result future is `future` is the only one on the wire.
    fn alone(&self, future: &Bound<'_, PyAny>) -> bool {
        self.count.load(Ordering::Relaxed) == 1
            && self.sum.load(Ordering::Relaxed) == future.as_ptr() as usize
    }

    /// The connection uses TLS (`options` as the DSN set it, `host` its server name).
    pub fn use_tls(&self, options: &TlsOptions, host: &str) {
        let _ = self.tls.set((options.requiring_tls(), host.to_owned()));
    }

    /// Keep the cancel socket's writer registration in step with what it needs; detach it
    /// (unregister and close it) once its cancel has ended. Run after every poll.
    pub fn sync_hooks(&self, py: Python<'_>) {
        let mut guard = self.io.lock().unwrap_or_else(PoisonError::into_inner);
        let Some(io) = guard.as_mut() else { return };
        if io.run.ended() {
            if io.writer_on {
                let _ = io.remove_writer.call0(py);
            }
            let _ = io.detach.call0(py);
            *guard = None;
            return;
        }
        let want = io.run.readiness.wants_writer();
        if want != io.writer_on {
            let hook = if want {
                &io.add_writer
            } else {
                &io.remove_writer
            };
            let _ = hook.call0(py);
            io.writer_on = want;
        }
    }
}

/// The caller of the operation whose result future is `future` gave up: begin a cancel if
/// that operation is the only one on the wire, the session's key is known, and no other
/// cancel is in progress. Nothing is sent once the connection has ended (closed, terminated
/// or lost), nor by a fork()ed child. From now on, operations wait until the cancel ends.
pub fn begin(shared: &Arc<Shared>, future: &Bound<'_, PyAny>) -> Option<Cancel> {
    let cancels = &shared.cancels;
    if shared.inherited() || shared.dead() || cancels.active() || !cancels.alone(future) {
        return None;
    }
    let key = shared.io.get()?.backend_key()?;
    cancels.active.store(true, Ordering::Release);
    Some(Cancel {
        shared: shared.clone(),
        run: Arc::new(Run {
            key,
            state: AtomicU8::new(Run::OPENING),
            stop: Notify::new(),
            readiness: Arc::default(),
        }),
    })
}

/// One cancel, from [`begin`] until it ends.
struct Run {
    /// Process id and secret key.
    key: (i32, i32),
    state: AtomicU8,
    /// Notified by [`Cancel::end`]: Python gave up before the request was written.
    stop: Notify,
    /// The cancel socket's readiness, fed by its loop callbacks.
    readiness: Arc<IoState>,
}

impl Run {
    /// Python is connecting the socket.
    const OPENING: u8 = 0;
    /// The task is negotiating TLS: the request isn't written yet.
    const SENDING: u8 = 1;
    /// The server may act on the request at any time: the cancel ends when it closes the
    /// socket (or the connection ends).
    const WRITTEN: u8 = 2;
    const ENDED: u8 = 3;

    fn ended(&self) -> bool {
        self.state.load(Ordering::Acquire) == Self::ENDED
    }

    /// The cancel is over: operations may send again.
    fn end(&self, cancels: &Cancels) {
        if self.state.swap(Self::ENDED, Ordering::AcqRel) != Self::ENDED {
            cancels.settle();
        }
    }
}

/// Ends the cancel when the task finishes (or is dropped, e.g. with a closed loop's tasks).
struct Ending<'a> {
    run: &'a Run,
    cancels: &'a Cancels,
}

impl Drop for Ending<'_> {
    fn drop(&mut self) {
        self.run.end(self.cancels);
    }
}

/// The asyncio registrations of the cancel socket: `functools.partial`s made in Python.
struct CancelIo {
    add_writer: Py<PyAny>,
    remove_writer: Py<PyAny>,
    /// Unregisters the socket from the loop and closes it.
    detach: Py<PyAny>,
    writer_on: bool,
    run: Arc<Run>,
}

/// A cancel in progress, from `Connection.begin_cancel` (see the module docs).
#[pyclass(frozen, module = "fastpg._native")]
pub struct Cancel {
    shared: Arc<Shared>,
    run: Arc<Run>,
}

#[pymethods]
impl Cancel {
    /// Send the request over `fd`, a non-blocking socket connected to the connection's
    /// address that Python keeps open (its reader already registered, with `on_readable`);
    /// `add_writer` / `remove_writer` toggle its writer registration (`on_writable`), and
    /// `detach` unregisters and closes it once the cancel is over. `sent` resolves once the
    /// request is written, or fails (TLS failure, connection lost) before.
    #[pyo3(signature = (fd, *, unix, add_writer, remove_writer, detach, sent))]
    #[allow(clippy::too_many_arguments)]
    fn send(
        &self,
        py: Python<'_>,
        fd: i32,
        unix: bool,
        add_writer: Py<PyAny>,
        remove_writer: Py<PyAny>,
        detach: Py<PyAny>,
        sent: Py<PyAny>,
    ) {
        let shared = &self.shared;
        let opened = self
            .run
            .state
            .compare_exchange(
                Run::OPENING,
                Run::SENDING,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok();
        let sent = plain_request(sent);
        if !opened || shared.dead() {
            // Given up already, or the connection has ended: send nothing.
            let _ = detach.call0(py);
            self.run.end(&shared.cancels);
            shared.post(sent, Outcome::Failed(shared.closed_error()));
            shared.pump(py);
            return;
        }
        let (pid, secret) = self.run.key;
        let mut request = BytesMut::new();
        frontend::cancel_request(pid, secret, &mut request);
        let tls = if unix {
            None
        } else {
            shared.cancels.tls.get().cloned()
        };
        let socket = LoopSocket::new(fd, unix, self.run.readiness.clone());
        let io = CancelIo {
            add_writer,
            remove_writer,
            detach,
            writer_on: false,
            run: self.run.clone(),
        };
        *shared
            .cancels
            .io
            .lock()
            .unwrap_or_else(PoisonError::into_inner) = Some(io);
        shared.spawn(task(
            shared.clone(),
            self.run.clone(),
            socket,
            tls,
            request,
            sent,
        ));
        shared.pump(py);
    }

    /// Python gave up on this cancel (its connect failed or timed out, or the TLS handshake
    /// timed out): it ends now, unless its request was written (then it ends when the server
    /// closes the socket). Safe to call more than once.
    fn end(&self, py: Python<'_>) {
        match self.run.state.load(Ordering::Acquire) {
            Run::OPENING => self.run.end(&self.shared.cancels),
            Run::SENDING => self.run.stop.notify_one(),
            _ => return,
        }
        self.shared.pump(py);
    }

    /// The cancel socket is readable (`loop.add_reader` callback).
    fn on_readable(&self, py: Python<'_>) {
        self.run.readiness.on_readable();
        self.shared.exec.rehome();
        self.shared.pump(py);
    }

    /// The cancel socket is writable (`loop.add_writer` callback).
    fn on_writable(&self, py: Python<'_>) {
        self.run.readiness.on_writable();
        self.shared.exec.rehome();
        self.shared.pump(py);
    }
}

/// Deliver the request unless Python gives up first or the connection ends; the cancel ends
/// with the task.
async fn task(
    shared: Arc<Shared>,
    run: Arc<Run>,
    socket: LoopSocket,
    tls: Option<(TlsOptions, String)>,
    request: BytesMut,
    sent: Arc<Request>,
) {
    let _ending = Ending {
        run: &run,
        cancels: &shared.cancels,
    };
    let delivery = deliver(&shared, &run, socket, tls.as_ref(), &request, &sent);
    let delivered = shared
        .unless_dead(async {
            tokio::select! {
                biased;
                // Only notified before the request is written.
                () = run.stop.notified() => Err(QueryError::Interface("cancel abandoned".into())),
                result = delivery => result,
            }
        })
        .await;
    if let Err(e) = delivered {
        shared.post(sent, Outcome::Failed(e));
    }
}

/// TLS if the connection uses it, then [`send_request`].
async fn deliver(
    shared: &Shared,
    run: &Run,
    socket: LoopSocket,
    tls: Option<&(TlsOptions, String)>,
    request: &[u8],
    sent: &Arc<Request>,
) -> Result<(), QueryError> {
    let Some((options, host)) = tls else {
        return send_request(shared, run, socket, request, sent).await;
    };
    // A scratch config: it only takes the server certificate's hash (for channel binding).
    let mut cfg = Config::new();
    match tls::start(socket, options, host, &mut cfg).await? {
        tls::Stream::Tls(stream) => send_request(shared, run, *stream, request, sent).await,
        // TLS is required (`Cancels::use_tls`), so `start` never returns a plain stream.
        tls::Stream::Plain(_) => Err(QueryError::Interface(
            "server does not support SSL, but SSL was required".into(),
        )),
    }
}

/// Write the request, then wait for the server to close the socket.
async fn send_request<Io: AsyncIo>(
    shared: &Shared,
    run: &Run,
    mut io: Io,
    request: &[u8],
    sent: &Arc<Request>,
) -> Result<(), QueryError> {
    let failed = |e: io::Error| QueryError::Interface(format!("cancel request failed: {e}"));
    // From the first byte on, the server may act on it.
    run.state.store(Run::WRITTEN, Ordering::Release);
    tls::write_all(&mut io, request).await.map_err(failed)?;
    flush(&mut io).await.map_err(failed)?;
    shared.post(sent.clone(), Outcome::Done);
    closed(&mut io).await;
    Ok(())
}

/// Flush what the stream buffers (TLS records), waiting for the socket as needed.
async fn flush<Io: AsyncIo>(io: &mut Io) -> io::Result<()> {
    loop {
        match io.flush() {
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                io.ready(Interest::WRITABLE).await?;
            }
            result => return result,
        }
    }
}

/// Read until the server closes the socket: EOF, or an error such as a reset.
async fn closed<Io: AsyncIo>(io: &mut Io) {
    let mut buf = [0u8; 64];
    loop {
        match io.read(&mut buf) {
            Ok(0) => return,
            Ok(_) => {}
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                if io.ready(Interest::READABLE).await.is_err() {
                    return;
                }
            }
            Err(_) => return,
        }
    }
}
