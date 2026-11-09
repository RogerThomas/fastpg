//! Connection setup with server-close detection.
//!
//! xitca-postgres 0.4's driver, on reading EOF, only marks its read half closed and then
//! waits for the client to write again. A connection closed by the server (restart,
//! `pg_terminate_backend`, idle timeout) is therefore never noticed, and a query sent
//! afterwards can wait forever for a response. Python connects the socket (to the hosts
//! [`targets`] lists), and we wrap it in [`EofWatch`] (which signals when a read returns EOF)
//! before handing it to `Postgres::connect_io`; the connection task then drops the driver on
//! EOF, which fails all pending requests promptly. A FATAL `ErrorResponse` counts as EOF: the
//! server closes the connection right after it, but that close can be lost, and the error
//! itself only fails the request it answers.
//!
//! TLS (`tls.rs`) sits between the socket and the watch: `EofWatch<TlsStream<socket>>`, so
//! the watch sees plaintext (a `close_notify` is a 0-byte read, a TLS error a fatal one).
//!
//! The driver also waits for the server's EOF after writing Terminate, which a stalled or
//! unreachable server never sends. So [`EofWatch`] follows the messages written too, and once
//! a complete Terminate has been written and flushed it reports EOF itself: the driver then
//! shuts the stream down (a TLS `close_notify`, then the socket's write half) and ends.
//!
//! xitca keeps `Host` in a private module, so hosts are read from their `Debug` form
//! (`Tcp("db")`, `Unix("/run/postgresql")`). If that ever stops parsing (e.g. after an
//! upgrade), [`targets`] fails with an `InterfaceError` naming the host (the
//! `hosts_parse_from_xitca_debug_output` test catches that).

use std::collections::VecDeque;
use std::io;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock, PoisonError};
use std::task::{Context, Poll};

use tokio::sync::Notify;
use xitca_io::io::{AsyncIo, Interest, Ready};
use xitca_postgres::error::DriverIoErrorMulti;
use xitca_postgres::{Client, Config, Driver, Postgres};

use crate::auth;
use crate::errors::QueryError;
use crate::pgenv::{self, Env, Passfile};
use crate::tls::{self, ChannelBinding, DsnOptions, SocketOptions, TlsOptions};

/// One response cycle (the server's messages up to and including a `ReadyForQuery`, i.e. the
/// answer to one `Sync` or simple query), summarised for a bulk write (see [`IoWatch::tap`]).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Cycle {
    /// Statements that completed (`CommandComplete` or `EmptyQueryResponse`).
    pub completed: usize,
    /// Sum of the `CommandComplete` row counts.
    pub affected: u64,
    /// An `ErrorResponse` arrived (after `completed` statements).
    pub failed: bool,
}

/// Cycles recorded while a bulk write collects them.
#[derive(Default)]
struct Tap {
    done: VecDeque<Cycle>,
    current: Cycle,
    /// `current` was already handed out (an error, before its `ReadyForQuery` arrived).
    claimed: bool,
}

/// Signals from the socket wrapper: server EOF, how many bytes were written so far (COPY uses
/// that for backpressure, since xitca's write buffer is not observable), the response cycles
/// a bulk write collects, and the session's cancel key.
#[derive(Default)]
pub struct IoWatch {
    pub eof: Notify,
    /// Wakes waiters after every successful write.
    pub progress: Notify,
    written: AtomicU64,
    /// The startup is over: every message written from now on is tagged (see [`Writes`]).
    started: AtomicBool,
    collecting: AtomicBool,
    tap: Mutex<Tap>,
    /// The process id and secret key of the server's `BackendKeyData`, read as the startup
    /// passes through (xitca keeps its copy private): what a CancelRequest names.
    key: OnceLock<(i32, i32)>,
    /// The session runs over TLS.
    encrypted: bool,
}

impl IoWatch {
    /// The session's `BackendKeyData`: process id and secret key (none from a server that
    /// sent none, or a key longer than protocol 3.0's 4 bytes).
    pub fn backend_key(&self) -> Option<(i32, i32)> {
        self.key.get().copied()
    }

    pub fn encrypted(&self) -> bool {
        self.encrypted
    }

    /// Bytes written to the socket so far.
    pub fn written(&self) -> u64 {
        self.written.load(Ordering::Acquire)
    }

    fn wrote(&self, n: usize) {
        if n > 0 {
            self.written.fetch_add(n as u64, Ordering::Release);
            self.progress.notify_waiters();
        }
    }

    fn tap(&self) -> std::sync::MutexGuard<'_, Tap> {
        self.tap.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Record the response cycles read from now on, for [`take_cycle`](Self::take_cycle).
    ///
    /// xitca 0.4 hands a request only the *last* `CommandComplete` of its cycle, so a bulk
    /// write that pipelines many statements behind one `Sync` reads the per-statement results
    /// here instead. Call it while no request of the caller's is outstanding: every cycle
    /// that then begins is either the answer to one of its requests, in order, or foreign
    /// (see [`Framer`]), which is skipped. So is a cycle that began before: xitca resolves
    /// some requests (a prepare, its type lookups) before reading their `ReadyForQuery`.
    pub fn start_collecting(&self) {
        *self.tap() = Tap::default();
        self.collecting.store(true, Ordering::Release);
    }

    pub fn stop_collecting(&self) {
        self.collecting.store(false, Ordering::Release);
        *self.tap() = Tap::default();
    }

    /// The oldest cycle not handed out yet. A request whose response xitca already returned
    /// was read through the tap, so its cycle is here: complete if the request succeeded,
    /// possibly still in progress if it failed (xitca reports an `ErrorResponse` as soon as it
    /// is parsed, before the `ReadyForQuery` that ends the cycle).
    pub fn take_cycle(&self) -> Option<Cycle> {
        let mut tap = self.tap();
        if let Some(cycle) = tap.done.pop_front() {
            return Some(cycle);
        }
        if tap.claimed || tap.current == Cycle::default() {
            return None;
        }
        tap.claimed = true;
        Some(tap.current)
    }

    /// A complete message of a cycle that began while collecting (`first`: the cycle's first
    /// non-asynchronous message, `body`: collected for `CommandComplete` only).
    fn record(&self, tag: u8, first: Option<u8>, body: &[u8]) {
        let mut tap = self.tap();
        match tag {
            b'C' => {
                tap.current.completed += 1;
                tap.current.affected += affected_rows(body);
            }
            b'I' => tap.current.completed += 1,
            b'E' => tap.current.failed = true,
            b'Z' => {
                let cycle = std::mem::take(&mut tap.current);
                // A cycle that began with CloseComplete answers a statement `Close` fired by
                // a dropped statement (xitca's `StatementGuarded`), not the bulk write.
                let foreign = first == Some(b'3');
                if !std::mem::take(&mut tap.claimed) && !foreign {
                    tap.done.push_back(cycle);
                }
            }
            _ => {}
        }
    }
}

/// The row count at the end of a `CommandComplete` tag (`INSERT 0 5`, `UPDATE 3`), else 0.
fn affected_rows(body: &[u8]) -> u64 {
    let tag = body.strip_suffix(b"\0").unwrap_or(body);
    let last = tag.rsplit(|b| *b == b' ').next().unwrap_or_default();
    std::str::from_utf8(last)
        .ok()
        .and_then(|n| n.parse().ok())
        .unwrap_or(0)
}

/// Whether an `ErrorResponse` body ends the session: severity FATAL or PANIC (the `V` field,
/// never translated; `S` from a server that sends no `V`).
fn ends_session(body: &[u8]) -> bool {
    let mut severity = None;
    let mut fields = body;
    while let [code, rest @ ..] = fields
        && *code != 0
    {
        let Some(end) = rest.iter().position(|b| *b == 0) else {
            break;
        };
        match code {
            b'V' => severity = Some(&rest[..end]),
            b'S' => severity = severity.or(Some(&rest[..end])),
            _ => {}
        }
        fields = &rest[end + 1..];
    }
    matches!(severity, Some(b"FATAL" | b"PANIC"))
}

/// Follows the message framing of everything read from the server (always: it is cheap, a
/// header per message, and so it is at a message boundary whenever a bulk write starts
/// collecting), and reports to [`IoWatch::record`] the complete messages of the cycles that
/// began while it collects. It also keeps the `BackendKeyData` of the startup.
///
/// Whether a cycle is reported is decided once, when its first non-asynchronous message
/// arrives: a cycle already under way when collecting starts is never reported, however
/// its remaining messages are split across reads.
#[derive(Default)]
struct Framer {
    /// The current message's header (tag + 4-byte length), as far as read.
    header: [u8; 5],
    header_len: usize,
    /// Body bytes of the current message still to come.
    remaining: usize,
    /// The current cycle's first non-asynchronous message.
    first: Option<u8>,
    /// The current cycle began while collecting (latched with `first`).
    began_collecting: bool,
    /// The current message is reported: its cycle began while collecting, and still is.
    reporting: bool,
    /// The body of a reported `CommandComplete`, of a `BackendKeyData` or of an
    /// `ErrorResponse`.
    body: Vec<u8>,
    /// The stream didn't parse as messages: stop following it (xitca fails too).
    lost: bool,
}

impl Framer {
    fn feed(&mut self, mut data: &[u8], watch: &IoWatch) {
        while !data.is_empty() && !self.lost {
            if self.header_len < self.header.len() {
                let take = (self.header.len() - self.header_len).min(data.len());
                self.header[self.header_len..self.header_len + take].copy_from_slice(&data[..take]);
                self.header_len += take;
                data = &data[take..];
                if self.header_len < self.header.len() {
                    return;
                }
                let [_, len @ ..] = self.header;
                let len = i32::from_be_bytes(len);
                let Some(body_len) = usize::try_from(len).ok().and_then(|n| n.checked_sub(4))
                else {
                    self.lost = true;
                    return;
                };
                self.remaining = body_len;
                self.start_message(watch);
            }
            let take = self.remaining.min(data.len());
            let tag = self.header[0];
            if (self.reporting && tag == b'C') || tag == b'K' || tag == b'E' {
                self.body.extend_from_slice(&data[..take]);
            }
            self.remaining -= take;
            data = &data[take..];
            if self.remaining == 0 {
                self.complete(watch);
            }
        }
    }

    /// A message header is complete: decide whether the message is reported.
    fn start_message(&mut self, watch: &IoWatch) {
        let tag = self.header[0];
        let collecting = watch.collecting.load(Ordering::Acquire);
        // NoticeResponse, ParameterStatus and NotificationResponse can arrive at any time.
        if !matches!(tag, b'N' | b'S' | b'A') && self.first.is_none() {
            self.first = Some(tag);
            self.began_collecting = collecting;
        }
        self.reporting = self.began_collecting && collecting;
        self.body.clear();
    }

    fn complete(&mut self, watch: &IoWatch) {
        let tag = self.header[0];
        self.header_len = 0;
        if self.reporting && matches!(tag, b'C' | b'I' | b'E' | b'Z') {
            watch.record(tag, self.first, &self.body);
        }
        if tag == b'K'
            && let Ok([p0, p1, p2, p3, k0, k1, k2, k3]) = <[u8; 8]>::try_from(self.body.as_slice())
        {
            let pid = i32::from_be_bytes([p0, p1, p2, p3]);
            let _ = watch.key.set((pid, i32::from_be_bytes([k0, k1, k2, k3])));
        }
        if tag == b'E' && ends_session(&self.body) {
            // The server closes the connection right after a FATAL error. Don't wait for the
            // close to arrive: it can be lost (macOS drops the RST of a backend that exits
            // with queries unread), and the queries behind the one the error answers would
            // then wait forever.
            watch.eof.notify_one();
            watch.progress.notify_waiters();
        }
        if tag == b'Z' {
            self.first = None;
            self.began_collecting = false;
        }
    }
}

/// Follows the framing of the messages written after the startup, to notice the session's
/// end: xitca writes Terminate (`X`) when the client is dropped, and nothing after it.
#[derive(Default)]
struct Writes {
    /// The current message's header (tag + 4-byte length), as far as written.
    header: [u8; 5],
    header_len: usize,
    /// Body bytes of the current message still to come.
    remaining: usize,
    /// The last complete message written is a Terminate.
    terminated: bool,
    /// ... and was flushed: the session is over, and reads report EOF.
    over: bool,
    /// The bytes didn't parse as messages: stop following them (closing then waits for the
    /// server's EOF, as xitca does).
    lost: bool,
}

impl Writes {
    fn feed(&mut self, mut data: &[u8]) {
        while !data.is_empty() && !self.lost {
            if self.header_len < self.header.len() {
                let take = (self.header.len() - self.header_len).min(data.len());
                self.header[self.header_len..self.header_len + take].copy_from_slice(&data[..take]);
                self.header_len += take;
                data = &data[take..];
                if self.header_len < self.header.len() {
                    return;
                }
                let [_, len @ ..] = self.header;
                let len = i32::from_be_bytes(len);
                let Some(body_len) = usize::try_from(len).ok().and_then(|n| n.checked_sub(4))
                else {
                    self.lost = true;
                    return;
                };
                self.remaining = body_len;
            }
            let take = self.remaining.min(data.len());
            self.remaining -= take;
            data = &data[take..];
            if self.remaining == 0 {
                self.header_len = 0;
                self.terminated = self.header[0] == b'X';
            }
        }
    }

    /// A flush succeeded: everything written so far is on its way.
    fn flushed(&mut self) {
        if self.terminated {
            self.over = true;
        }
    }
}

/// Transparent `AsyncIo` wrapper that fires `eof` when the peer closes the stream, counts
/// written bytes, follows the messages read (for [`IoWatch::take_cycle`]), and reports EOF
/// once Terminate is written (see [`Writes`]).
pub struct EofWatch<Io> {
    io: Io,
    eof: Arc<IoWatch>,
    framer: Framer,
    writes: Writes,
}

/// Whether an I/O error means the stream is unusable (as opposed to "try again").
fn is_fatal(e: &io::Error) -> bool {
    !matches!(
        e.kind(),
        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
    )
}

impl<Io> EofWatch<Io> {
    fn signal_eof(&self) {
        self.eof.eof.notify_one();
        self.eof.progress.notify_waiters();
    }

    /// `n` bytes of `buf` were written.
    fn wrote(&mut self, buf: &[u8], n: usize) {
        self.eof.wrote(n);
        if self.eof.started.load(Ordering::Acquire) {
            self.writes.feed(&buf[..n]);
        }
    }
}

impl<Io: io::Read> io::Read for EofWatch<Io> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.writes.over {
            // Terminate is on its way: nothing the server sends now matters, and a server
            // that stopped answering would never close. No `eof` signal: the driver is left
            // to shut the stream down (`poll_shutdown`) and end by itself.
            return Ok(0);
        }
        // A peer close shows up as a 0-byte read, or (on Linux, once our write after the
        // server's FIN has drawn an RST) as ECONNRESET: both end the connection.
        match self.io.read(buf) {
            Ok(0) if !buf.is_empty() => {
                self.signal_eof();
                Ok(0)
            }
            Ok(n) => {
                self.framer.feed(&buf[..n], &self.eof);
                Ok(n)
            }
            Err(e) if is_fatal(&e) => {
                self.signal_eof();
                Err(e)
            }
            other => other,
        }
    }
}

impl<Io: io::Write> io::Write for EofWatch<Io> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self.io.write(buf) {
            Ok(n) => {
                self.wrote(buf, n);
                Ok(n)
            }
            Err(e) => {
                if is_fatal(&e) {
                    self.signal_eof(); // EPIPE / ECONNRESET: the server is gone
                }
                Err(e)
            }
        }
    }

    fn write_vectored(&mut self, bufs: &[io::IoSlice<'_>]) -> io::Result<usize> {
        match self.io.write_vectored(bufs) {
            Ok(n) => {
                let mut left = n;
                for buf in bufs {
                    let part = left.min(buf.len());
                    self.wrote(buf, part);
                    left -= part;
                }
                Ok(n)
            }
            Err(e) => {
                if is_fatal(&e) {
                    self.signal_eof();
                }
                Err(e)
            }
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        // A no-op on a plain socket; over TLS it writes the last records.
        let result = self.io.flush();
        match &result {
            Ok(()) => self.writes.flushed(),
            Err(e) if is_fatal(e) => self.signal_eof(),
            Err(_) => {}
        }
        result
    }
}

impl<Io: AsyncIo + Send> AsyncIo for EofWatch<Io> {
    fn ready(&mut self, interest: Interest) -> impl Future<Output = io::Result<Ready>> + Send {
        // Readable at once when over: the read reports EOF.
        let over = self.writes.over && interest.is_readable();
        let io = &mut self.io;
        async move {
            if over {
                return Ok(Ready::READABLE);
            }
            io.ready(interest).await
        }
    }

    fn poll_ready(&mut self, interest: Interest, cx: &mut Context<'_>) -> Poll<io::Result<Ready>> {
        if self.writes.over && interest.is_readable() {
            return Poll::Ready(Ok(Ready::READABLE));
        }
        self.io.poll_ready(interest, cx)
    }

    fn is_vectored_write(&self) -> bool {
        self.io.is_vectored_write()
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Pin::new(&mut self.get_mut().io).poll_shutdown(cx)
    }
}

enum Target {
    Tcp(String),
    Unix(PathBuf),
}

/// Parse `dsn` into xitca's config and the settings xitca never sees (TLS, socket options),
/// with libpq's environment variables for the keys it leaves out.
pub fn config(dsn: &str) -> Result<(Config, DsnOptions), QueryError> {
    config_with(dsn, &pgenv::process_env, &pgenv::os_user)
}

/// Looks up the default user: [`pgenv::os_user`], or a fixed one in tests.
type OsUser<'a> = &'a dyn Fn() -> Result<String, String>;

fn config_with(dsn: &str, env: Env, os_user: OsUser) -> Result<(Config, DsnOptions), QueryError> {
    let (rest, options) = tls::split_dsn(dsn, env)?;
    // Every key is known by now (`split_dsn` names an unknown one), and xitca's parse errors
    // carry no detail at all ("WIP error type ...").
    let cfg = Config::try_from(rest.as_ref()).map_err(|_| {
        QueryError::Interface(
            "invalid connection string: malformed, or an invalid port value".to_owned(),
        )
    })?;
    let cfg = fill_from(cfg, options.port_set, env, os_user)?;
    Ok((cfg, options))
}

/// libpq's environment variables ("Environment Variables" in the libpq docs) for the keys
/// xitca parses, where the DSN leaves them out (or gives them empty). With no user from
/// either, the OS user's login name, as in libpq (the server's default database follows it).
fn fill_from(
    mut cfg: Config,
    port_set: bool,
    env: Env,
    os_user: OsUser,
) -> Result<Config, QueryError> {
    let unset = |value: Option<&str>| value.is_none_or(str::is_empty);
    if unset(cfg.get_user()) {
        let user = match env("PGUSER") {
            Some(user) => user,
            None => os_user().map_err(QueryError::Interface)?,
        };
        cfg.user(&user);
    }
    if cfg.get_password().is_none_or(<[u8]>::is_empty)
        && let Some(password) = env("PGPASSWORD")
    {
        cfg.password(password);
    }
    if unset(cfg.get_dbname())
        && let Some(dbname) = env("PGDATABASE")
    {
        cfg.dbname(&dbname);
    }
    if unset(cfg.get_options())
        && let Some(options) = env("PGOPTIONS")
    {
        cfg.options(&options);
    }
    if unset(cfg.get_application_name())
        && let Some(name) = env("PGAPPNAME")
    {
        cfg.application_name(&name);
    }
    if cfg.get_hosts().is_empty()
        && let Some(hosts) = env("PGHOST")
    {
        for host in hosts.split(',') {
            cfg.host(host);
        }
    }
    if !port_set && let Some(value) = env("PGPORT") {
        // As xitca reads the `port` key: a list, an empty entry meaning 5432.
        let ports = value
            .split(',')
            .map(|port| match port {
                "" => Ok(5432),
                _ => port.parse::<u16>(),
            })
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| QueryError::Interface(format!("invalid PGPORT value {value:?}")))?;
        cfg = with_ports(&cfg, &ports)?;
    }
    Ok(cfg)
}

/// `cfg` with `ports` instead of its own: a URL host without a port still gets 5432 from
/// xitca's parser, and xitca can only add ports.
fn with_ports(cfg: &Config, ports: &[u16]) -> Result<Config, QueryError> {
    let mut out = Config::new();
    if let Some(user) = cfg.get_user() {
        out.user(user);
    }
    if let Some(password) = cfg.get_password() {
        out.password(password);
    }
    if let Some(dbname) = cfg.get_dbname() {
        out.dbname(dbname);
    }
    if let Some(options) = cfg.get_options() {
        out.options(options);
    }
    if let Some(name) = cfg.get_application_name() {
        out.application_name(name);
    }
    // The other settings xitca keeps are never given to it (`split_dsn` takes their keys).
    for host in cfg.get_hosts() {
        match parse_host(&format!("{host:?}")) {
            Some(Target::Tcp(name)) => out.host(&name),
            Some(Target::Unix(path)) => out.host_path(path),
            None => return Err(QueryError::Interface(format!("unsupported host {host:?}"))),
        };
    }
    for &port in ports {
        out.port(port);
    }
    Ok(out)
}

/// [`config`] for a connection to `host` (as the DSN gives it: a name, an address or a
/// socket directory) and `port`, with the password from the password file if neither the
/// DSN nor `PGPASSWORD` gives one. Also returns a warning when the password file is ignored
/// (its permissions are too open, or it is not a file).
pub fn config_for(
    dsn: &str,
    host: &str,
    port: u16,
) -> Result<(Config, DsnOptions, Option<String>), QueryError> {
    let (mut cfg, options) = config(dsn)?;
    if cfg.get_password().is_some_and(|p| !p.is_empty()) {
        return Ok((cfg, options, None));
    }
    let Some(path) = options.passfile.clone().or_else(pgenv::default_passfile) else {
        return Ok((cfg, options, None));
    };
    let user = cfg.get_user().unwrap_or_default();
    // The server's default database is the user's name, as is libpq's.
    let dbname = cfg.get_dbname().filter(|d| !d.is_empty()).unwrap_or(user);
    let warning = match pgenv::lookup(&path, host, port, dbname, user) {
        Passfile::Password(password) => {
            cfg.password(password);
            None
        }
        Passfile::Nothing => None,
        Passfile::Ignored(warning) => Some(warning),
    };
    Ok((cfg, options, warning))
}

/// libpq's keys of `dsn` applied in Python (after checking the whole DSN).
pub fn socket_options(dsn: &str) -> Result<SocketOptions, QueryError> {
    Ok(config(dsn)?.1.socket)
}

/// The hosts of `dsn` as `("tcp" | "unix", host or directory/path, port)`, in connection
/// order, for Python to connect the socket to. As in libpq, host N goes with port N; a single
/// port goes with every host, and no port means 5432. No host means the local unix socket,
/// in the first of `pgenv::DEFAULT_SOCKET_DIRS` that holds it.
pub fn targets(dsn: &str) -> Result<Vec<(&'static str, String, u16)>, QueryError> {
    targets_of(&config(dsn)?.0, pgenv::DEFAULT_SOCKET_DIRS)
}

fn targets_of(
    cfg: &Config,
    socket_dirs: &[&str],
) -> Result<Vec<(&'static str, String, u16)>, QueryError> {
    let (hosts, ports) = (cfg.get_hosts(), cfg.get_ports());
    let host_count = hosts.len().max(1);
    if ports.len() > 1 && ports.len() != host_count {
        return Err(QueryError::Interface(format!(
            "could not match {} port numbers to {host_count} hosts",
            ports.len(),
        )));
    }
    if hosts.is_empty() {
        let port = ports.first().copied().unwrap_or(5432);
        let dir = pgenv::socket_dir(socket_dirs, port)
            .map_err(|e| QueryError::Connect(format!("could not connect to server: {e}")))?;
        return Ok(vec![("unix", dir.to_owned(), port)]);
    }
    let mut out = Vec::new();
    for (i, host) in hosts.iter().enumerate() {
        let target = parse_host(&format!("{host:?}"))
            .ok_or_else(|| QueryError::Interface(format!("unsupported host {host:?}")))?;
        let port = match ports {
            [] => 5432,
            [port] => *port,
            _ => ports[i],
        };
        out.push(match target {
            Target::Tcp(name) => ("tcp", name, port),
            Target::Unix(path) => ("unix", path.to_string_lossy().into_owned(), port),
        });
    }
    Ok(out)
}

/// `Tcp("db.local")` / `Unix("/tmp")` → [`Target`]; `None` for anything unexpected.
fn parse_host(debug: &str) -> Option<Target> {
    fn inner<'a>(s: &'a str, prefix: &str) -> Option<&'a str> {
        let quoted = s.strip_prefix(prefix)?.strip_suffix(')')?;
        let text = quoted.strip_prefix('"')?.strip_suffix('"')?;
        // Only accept strings that Debug didn't escape.
        (!text.contains('\\')).then_some(text)
    }
    if let Some(name) = inner(debug, "Tcp(") {
        return Some(Target::Tcp(name.to_owned()));
    }
    inner(debug, "Unix(").map(|path| Target::Unix(PathBuf::from(path)))
}

/// TLS negotiation as `tls` asks (TCP only: like libpq, never over a unix socket, for which
/// `host` is `None`), then PostgreSQL startup over the connected `io`. Returns the client,
/// its driver, and a watch whose `eof` fires when the server closes the connection.
pub async fn handshake<Io: AsyncIo + Send + 'static>(
    io: Io,
    mut cfg: Config,
    tls: &TlsOptions,
    host: Option<&str>,
) -> Result<(Client, Driver, Arc<IoWatch>), QueryError> {
    let binding = tls.channel_binding();
    let Some(host) = host else {
        return login(io, cfg, binding, false).await;
    };
    match tls::start(io, tls, host, &mut cfg).await? {
        tls::Stream::Plain(io) => login(io, cfg, binding, false).await,
        tls::Stream::Tls(io) => login(*io, cfg, binding, true).await,
    }
}

/// Startup, authenticated by xitca (`channel_binding=prefer`) or first by `auth.rs`.
async fn login<Io: AsyncIo + Send + 'static>(
    io: Io,
    cfg: Config,
    binding: ChannelBinding,
    encrypted: bool,
) -> Result<(Client, Driver, Arc<IoWatch>), QueryError> {
    if binding == ChannelBinding::Prefer {
        return startup(io, cfg, encrypted).await;
    }
    let io = auth::authenticate(io, &cfg, binding, encrypted).await?;
    startup(io, cfg, encrypted).await
}

/// A failed startup: the socket failing, or the server closing it, means the connection
/// couldn't be established (`ConnectError`). A server error (e.g. a wrong password) is kept.
fn startup_error(e: xitca_postgres::Error) -> QueryError {
    if e.downcast_ref::<io::Error>().is_some() || e.downcast_ref::<DriverIoErrorMulti>().is_some() {
        return QueryError::Connect(e.to_string());
    }
    if e.is_driver_down() {
        return QueryError::Connect(format!("connection lost: {e}"));
    }
    QueryError::Pg(e)
}

async fn startup<Io: AsyncIo + Send + 'static>(
    io: Io,
    cfg: Config,
    encrypted: bool,
) -> Result<(Client, Driver, Arc<IoWatch>), QueryError> {
    let eof = Arc::new(IoWatch {
        encrypted,
        ..IoWatch::default()
    });
    let watched = EofWatch {
        io,
        eof: eof.clone(),
        framer: Framer::default(),
        writes: Writes::default(),
    };
    let (client, driver) = Postgres::new(cfg)
        .connect_io(watched)
        .await
        .map_err(startup_error)?;
    // The startup wrote each message in full: the next byte written starts a message.
    eof.started.store(true, Ordering::Release);
    Ok((client, driver, eof))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn message(tag: u8, body: &[u8]) -> Vec<u8> {
        let mut out = vec![tag];
        out.extend_from_slice(&(body.len() as i32 + 4).to_be_bytes());
        out.extend_from_slice(body);
        out
    }

    fn complete(tag: &str) -> Vec<u8> {
        message(b'C', format!("{tag}\0").as_bytes())
    }

    fn ready() -> Vec<u8> {
        message(b'Z', b"T")
    }

    /// Feed `stream` to a fresh framer in pieces of `piece` bytes.
    fn feed(framer: &mut Framer, watch: &IoWatch, stream: &[u8], piece: usize) {
        for part in stream.chunks(piece) {
            framer.feed(part, watch);
        }
    }

    #[test]
    fn tap_summarises_cycles_however_the_stream_is_split() {
        let mut stream = Vec::new();
        // A cycle already under way when collecting starts is not the caller's (here a
        // prepare's, which xitca resolves at NoData, before its ReadyForQuery)...
        let parsed = [
            message(b'1', b""),
            message(b't', b"\0\0"),
            message(b'n', b""),
        ]
        .concat();
        let early = [parsed.clone(), ready()].concat();
        // ... nor is a statement Close's (CloseComplete first).
        let close = [message(b'3', b""), ready()].concat();
        let batch = [
            message(b'2', b""),
            complete("INSERT 0 1"),
            message(b'N', b"notice\0"),
            message(b'2', b""),
            message(b'D', b"\0\x01\0\0\0\x011"),
            complete("UPDATE 7"),
            message(b'S', b"application_name\0x\0"),
            message(b'2', b""),
            message(b'I', b""),
            ready(),
        ]
        .concat();
        let begin = [complete("BEGIN"), ready()].concat();
        stream.extend_from_slice(&close);
        stream.extend_from_slice(&begin);
        stream.extend_from_slice(&batch);
        for piece in [1, 2, 3, 7, 4096] {
            let watch = IoWatch::default();
            let mut framer = Framer::default();
            feed(&mut framer, &watch, &parsed, piece); // read before collecting
            watch.start_collecting();
            feed(&mut framer, &watch, &early[parsed.len()..], piece);
            feed(&mut framer, &watch, &stream, piece);
            let begin = Cycle {
                completed: 1,
                affected: 0,
                failed: false,
            };
            let batch = Cycle {
                completed: 3,
                affected: 8,
                failed: false,
            };
            assert_eq!(watch.take_cycle(), Some(begin), "piece {piece}");
            assert_eq!(watch.take_cycle(), Some(batch), "piece {piece}");
            assert_eq!(watch.take_cycle(), None, "piece {piece}");
        }
    }

    #[test]
    fn tap_skips_the_rest_of_a_cycle_that_began_before_collecting() {
        // E.g. a type lookup of xitca's, resolved at its last DataRow.
        let started = [message(b'2', b""), message(b'D', b"\0\x01\0\0\0\x011")].concat();
        let rest = [complete("SELECT 1"), ready()].concat();
        let ours = [message(b'2', b""), complete("INSERT 0 3"), ready()].concat();
        for split in 0..=rest.len() {
            let watch = IoWatch::default();
            let mut framer = Framer::default();
            framer.feed(&started, &watch);
            framer.feed(&rest[..split], &watch);
            watch.start_collecting();
            framer.feed(&rest[split..], &watch);
            assert_eq!(watch.take_cycle(), None, "split {split}");
            framer.feed(&ours, &watch);
            let insert = Cycle {
                completed: 1,
                affected: 3,
                failed: false,
            };
            assert_eq!(watch.take_cycle(), Some(insert), "split {split}");
            assert_eq!(watch.take_cycle(), None, "split {split}");
        }
    }

    #[test]
    fn tap_hands_out_a_failed_cycle_before_its_ready_for_query() {
        let watch = IoWatch::default();
        let mut framer = Framer::default();
        watch.start_collecting();
        let failing = [
            message(b'2', b""),
            complete("INSERT 0 1"),
            message(b'2', b""),
            message(b'E', b"SERROR\0\0"),
        ]
        .concat();
        framer.feed(&failing, &watch);
        let failed = Cycle {
            completed: 1,
            affected: 1,
            failed: true,
        };
        assert_eq!(watch.take_cycle(), Some(failed));
        assert_eq!(watch.take_cycle(), None); // claimed
        framer.feed(&ready(), &watch); // its ReadyForQuery: not handed out twice
        let next = [complete("ROLLBACK"), ready()].concat();
        framer.feed(&next, &watch);
        assert_eq!(watch.take_cycle().map(|c| c.completed), Some(1));
    }

    #[test]
    fn framer_keeps_the_backend_key_however_the_startup_is_split() {
        let startup = [
            message(b'R', b"\0\0\0\0"), // AuthenticationOk
            message(b'S', b"server_version\x0018\0"),
            message(b'K', &[0, 0, 0x30, 0x39, 0xde, 0xad, 0xbe, 0xef]),
            ready(),
        ]
        .concat();
        for piece in [1, 2, 3, 7, 4096] {
            let watch = IoWatch::default();
            let mut framer = Framer::default();
            feed(&mut framer, &watch, &startup, piece);
            let key = Some((12345, i32::from_be_bytes([0xde, 0xad, 0xbe, 0xef])));
            assert_eq!(watch.backend_key(), key, "piece {piece}");
        }
        // A protocol 3.2 key (longer than 4 bytes) can't go in a 16-byte request: none kept.
        let watch = IoWatch::default();
        let mut framer = Framer::default();
        framer.feed(&message(b'K', &[0; 36]), &watch);
        assert_eq!(watch.backend_key(), None);
    }

    #[test]
    fn a_fatal_error_ends_the_session_however_it_is_split() {
        let fatal = message(b'E', b"SFATAL\0VFATAL\0C57P01\0Mterminating connection\0\0");
        for piece in [1, 2, 3, 7, 4096] {
            let watch = IoWatch::default();
            let mut framer = Framer::default();
            feed(&mut framer, &watch, &fatal, piece);
            // The permit `eof` keeps: the connection task's next wait returns at once.
            let ended = watch.eof.notified();
            tokio::pin!(ended);
            assert!(ended.as_mut().enable(), "piece {piece}");
        }
        // Ordinary errors don't; nor does a translated `S` when `V` says ERROR.
        for body in [
            &b"SERROR\0VERROR\0C22012\0\0"[..],
            b"SFATALE\0VERROR\0\0",
            b"SERROR\0\0",
        ] {
            let watch = IoWatch::default();
            Framer::default().feed(&message(b'E', body), &watch);
            let ended = watch.eof.notified();
            tokio::pin!(ended);
            assert!(!ended.as_mut().enable());
        }
        assert!(ends_session(b"SPANIC\0\0")); // a server that sends no `V`
    }

    #[test]
    fn tap_records_nothing_unless_collecting() {
        let watch = IoWatch::default();
        let mut framer = Framer::default();
        framer.feed(&[complete("SELECT 1"), ready()].concat(), &watch);
        assert_eq!(watch.take_cycle(), None);
        watch.start_collecting();
        framer.feed(&[complete("SELECT 2"), ready()].concat(), &watch);
        watch.stop_collecting();
        framer.feed(&[complete("SELECT 3"), ready()].concat(), &watch);
        assert_eq!(watch.take_cycle(), None);
    }

    /// A stream whose reads always have server bytes, and whose writes take at most `cap`
    /// bytes at a time.
    struct Peer {
        cap: usize,
        written: Vec<u8>,
    }

    impl io::Read for Peer {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            buf[0] = b'Z';
            Ok(1)
        }
    }

    impl io::Write for Peer {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            let n = buf.len().min(self.cap);
            self.written.extend_from_slice(&buf[..n]);
            Ok(n)
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    fn watched(cap: usize) -> EofWatch<Peer> {
        let eof = Arc::new(IoWatch::default());
        eof.started.store(true, Ordering::Release);
        EofWatch {
            io: Peer {
                cap,
                written: Vec::new(),
            },
            eof,
            framer: Framer::default(),
            writes: Writes::default(),
        }
    }

    fn write_all(watch: &mut EofWatch<Peer>, mut data: &[u8]) {
        use std::io::Write;
        while !data.is_empty() {
            let n = watch.write(data).unwrap();
            data = &data[n..];
        }
    }

    #[test]
    fn a_flushed_terminate_ends_reads_however_the_writes_are_split() {
        use std::io::{Read, Write};
        // Bodies holding a Terminate's bytes are not a Terminate.
        let terminate = message(b'X', b"");
        let before = [
            message(
                b'P',
                &[b"\0SELECT $1\0\0\0".as_slice(), &terminate].concat(),
            ),
            message(b'd', &terminate),
            message(b'S', b""),
        ]
        .concat();
        for cap in [1, 2, 3, 5, 7, 4096] {
            let mut watch = watched(cap);
            write_all(&mut watch, &before);
            watch.flush().unwrap();
            let mut buf = [0u8; 16];
            assert_eq!(watch.read(&mut buf).unwrap(), 1, "cap {cap}");
            write_all(&mut watch, &terminate);
            assert_eq!(
                watch.read(&mut buf).unwrap(),
                1,
                "cap {cap}: not flushed yet"
            );
            watch.flush().unwrap();
            assert_eq!(watch.read(&mut buf).unwrap(), 0, "cap {cap}");
            assert_eq!(watch.io.written, [before.as_slice(), &terminate].concat());
        }
    }

    #[test]
    fn writes_during_the_startup_are_not_followed() {
        use std::io::{Read, Write};
        let mut watch = watched(4096);
        watch.eof.started.store(false, Ordering::Release);
        // An untagged startup message whose bytes end like a Terminate.
        write_all(&mut watch, &[0, 0, 0, 9, 0, 3, 0, 0, b'X']);
        write_all(&mut watch, &message(b'X', b""));
        watch.flush().unwrap();
        let mut buf = [0u8; 16];
        assert_eq!(watch.read(&mut buf).unwrap(), 1);
    }

    #[test]
    fn command_tags_give_row_counts() {
        assert_eq!(affected_rows(b"INSERT 0 12\0"), 12);
        assert_eq!(affected_rows(b"DELETE 3\0"), 3);
        assert_eq!(affected_rows(b"BEGIN\0"), 0);
        assert_eq!(affected_rows(b""), 0);
    }

    #[test]
    fn hosts_parse_from_xitca_debug_output() {
        let cfg = Config::try_from("postgresql://u:p@db.example:5433,/run/postgresql/x").unwrap();
        let parsed: Vec<_> = cfg
            .get_hosts()
            .iter()
            .map(|h| parse_host(&format!("{h:?}")))
            .collect();
        assert!(
            matches!(&parsed[0], Some(Target::Tcp(h)) if h == "db.example"),
            "{:?}",
            cfg.get_hosts()
        );
        assert_eq!(format!("{:?}", cfg.get_ssl_mode()), "Disable");
        let unix = Config::try_from("host=/run/postgresql user=u").unwrap();
        let parsed = parse_host(&format!("{:?}", unix.get_hosts()[0]));
        assert!(
            matches!(parsed, Some(Target::Unix(p)) if p == std::path::Path::new("/run/postgresql"))
        );
    }

    #[test]
    fn hosts_go_with_their_ports_as_in_libpq() {
        let tcp = |host: &str, port| ("tcp", host.to_owned(), port);
        let pairs = |dsn| targets_with(dsn, &[]).unwrap_or_else(|_| panic!("{dsn}"));
        let paired = vec![tcp("h1", 1111), tcp("h2", 2222)];
        assert_eq!(pairs("postgresql://u@h1:1111,h2:2222/db"), paired);
        assert_eq!(pairs("host=h1,h2 port=1111,2222 user=u"), paired);
        assert_eq!(
            pairs("postgresql://u@/db?host=h1,h2&port=1111,2222"),
            paired
        );
        // One port goes with every host; none means 5432 (as does an empty one).
        let one = vec![tcp("h1", 1111), tcp("h2", 1111)];
        assert_eq!(pairs("host=h1,h2 port=1111 user=u"), one);
        let default = vec![tcp("h1", 5432), tcp("h2", 5432)];
        assert_eq!(pairs("host=h1,h2 user=u"), default);
        assert_eq!(pairs("postgresql://u@h1,h2/db"), default);
        assert_eq!(
            pairs("host=h1,h2 port=,1111"),
            vec![tcp("h1", 5432), tcp("h2", 1111)]
        );
        assert_eq!(
            pairs("host=/run/pg,h2 port=1111,2222"),
            vec![("unix", "/run/pg".to_owned(), 1111), tcp("h2", 2222)]
        );
        for (dsn, message) in [
            (
                "host=h1,h2,h3 port=1,2",
                "could not match 2 port numbers to 3 hosts",
            ),
            (
                "host=h1 port=1,2",
                "could not match 2 port numbers to 1 hosts",
            ),
        ] {
            match targets_with(dsn, &[]) {
                Err(QueryError::Interface(m)) => assert_eq!(m, message),
                _ => panic!("expected an InterfaceError for {dsn:?}"),
            }
        }
    }

    fn config_env(dsn: &str, vars: &[(&str, &str)]) -> Result<Config, QueryError> {
        let env = |name: &str| {
            vars.iter()
                .find(|(var, _)| *var == name)
                .map(|(_, value)| (*value).to_owned())
        };
        Ok(config_with(dsn, &env, &|| Ok("osuser".to_owned()))?.0)
    }

    /// [`targets`] with the environment variables `vars` only, and no default socket
    /// directory.
    fn targets_with(
        dsn: &str,
        vars: &[(&str, &str)],
    ) -> Result<Vec<(&'static str, String, u16)>, QueryError> {
        targets_of(&config_env(dsn, vars)?, &[])
    }

    const ENV: &[(&str, &str)] = &[
        ("PGHOST", "envhost,/run/env"),
        ("PGPORT", "6001,6002"),
        ("PGUSER", "envuser"),
        ("PGPASSWORD", "envpass"),
        ("PGDATABASE", "envdb"),
        ("PGOPTIONS", "-c work_mem=7MB"),
        ("PGAPPNAME", "envapp"),
    ];

    #[test]
    fn an_empty_dsn_takes_everything_from_the_environment() {
        for dsn in ["", "postgresql://", "postgres:///"] {
            let cfg = config_env(dsn, ENV).unwrap_or_else(|_| panic!("{dsn}"));
            assert_eq!(cfg.get_user(), Some("envuser"));
            assert_eq!(cfg.get_password(), Some(&b"envpass"[..]));
            assert_eq!(cfg.get_dbname(), Some("envdb"));
            assert_eq!(cfg.get_options(), Some("-c work_mem=7MB"));
            assert_eq!(cfg.get_application_name(), Some("envapp"));
            assert_eq!(
                targets_of(&cfg, &[]).unwrap_or_default(),
                vec![
                    ("tcp", "envhost".to_owned(), 6001),
                    ("unix", "/run/env".to_owned(), 6002)
                ],
                "{dsn}"
            );
        }
        let cfg = config_env("", &[]).unwrap_or_else(|_| panic!("no environment"));
        let mut expected = Config::new();
        expected.user("osuser");
        assert_eq!(cfg, expected, "no environment: only the OS user is set");
    }

    #[test]
    fn with_no_user_the_os_user_is_used() {
        for dsn in ["", "host=h", "postgresql://h/d", "user='' host=h"] {
            let cfg = config_env(dsn, &[]).unwrap_or_else(|_| panic!("{dsn}"));
            assert_eq!(cfg.get_user(), Some("osuser"), "{dsn}");
        }
        let cfg =
            config_env("host=h", &[("PGUSER", "envuser")]).unwrap_or_else(|_| panic!("PGUSER"));
        assert_eq!(cfg.get_user(), Some("envuser"));
        // Only looked up when needed: a failing lookup doesn't matter then.
        let fails = || Err("could not look up local user ID 7: user does not exist".to_owned());
        let env = |_: &str| None;
        assert!(config_with("user=u", &env, &fails).is_ok());
        match config_with("host=h", &env, &fails) {
            Err(QueryError::Interface(m)) => {
                assert_eq!(m, "could not look up local user ID 7: user does not exist");
            }
            _ => panic!("expected an InterfaceError"),
        }
    }

    #[test]
    fn with_no_host_the_local_socket_is_used() {
        let base = std::env::temp_dir().join(format!("fastpg-nohost-{}", std::process::id()));
        let (empty, full) = (base.join("empty"), base.join("full"));
        for dir in [&empty, &full] {
            std::fs::create_dir_all(dir).unwrap_or_else(|e| panic!("{e}"));
        }
        for port in [5432, 6001] {
            std::fs::write(full.join(format!(".s.PGSQL.{port}")), "")
                .unwrap_or_else(|e| panic!("{e}"));
        }
        let (empty, full) = (empty.to_string_lossy(), full.to_string_lossy());
        let dirs = [empty.as_ref(), full.as_ref()];
        let targets = |dsn: &str, vars: &[(&str, &str)]| {
            targets_of(
                &config_env(dsn, vars).unwrap_or_else(|_| panic!("{dsn}")),
                &dirs,
            )
        };
        let pairs = |dsn: &str, vars: &[(&str, &str)]| {
            targets(dsn, vars).unwrap_or_else(|_| panic!("{dsn} {vars:?}"))
        };
        let unix = |port| vec![("unix", full.to_string(), port)];
        assert_eq!(pairs("", &[]), unix(5432));
        assert_eq!(pairs("user=u dbname=d", &[]), unix(5432));
        assert_eq!(pairs("postgresql:///d", &[]), unix(5432));
        assert_eq!(pairs("port=6001", &[]), unix(6001));
        assert_eq!(pairs("", &[("PGPORT", "6001")]), unix(6001));
        // A host from PGHOST or the DSN is used as before.
        assert_eq!(
            pairs("", &[("PGHOST", "h")]),
            vec![("tcp", "h".to_owned(), 5432)]
        );
        // No socket for the port: the error names each path tried.
        match targets("port=6002", &[]) {
            Err(QueryError::Connect(m)) => assert_eq!(
                m,
                format!(
                    "could not connect to server: no host given, and no local server socket \
                     found (tried {empty}/.s.PGSQL.6002, {full}/.s.PGSQL.6002); set host= or \
                     PGHOST"
                )
            ),
            _ => panic!("expected a ConnectError"),
        }
        // No host is one host, for matching ports.
        match targets("port=1,2", &[]) {
            Err(QueryError::Interface(m)) => {
                assert_eq!(m, "could not match 2 port numbers to 1 hosts");
            }
            _ => panic!("expected an InterfaceError"),
        }
        let _ = std::fs::remove_dir_all(base);
    }

    #[test]
    fn the_dsn_wins_over_the_environment() {
        for dsn in [
            "host=h port=1 user=u password=p dbname=d options=-cx=y application_name=a",
            "postgresql://u:p@h:1/d?options=-cx=y&application_name=a",
        ] {
            let cfg = config_env(dsn, ENV).unwrap_or_else(|_| panic!("{dsn}"));
            assert_eq!(cfg.get_user(), Some("u"));
            assert_eq!(cfg.get_password(), Some(&b"p"[..]));
            assert_eq!(cfg.get_dbname(), Some("d"));
            assert_eq!(cfg.get_options(), Some("-cx=y"));
            assert_eq!(cfg.get_application_name(), Some("a"));
            assert_eq!(
                targets_of(&cfg, &[]).unwrap_or_default(),
                vec![("tcp", "h".to_owned(), 1)]
            );
        }
    }

    #[test]
    fn pgport_goes_with_hosts_the_dsn_gives_no_port() {
        let env = [("PGPORT", "6001")];
        let tcp = |host: &str, port| ("tcp", host.to_owned(), port);
        let pairs = |dsn| targets_with(dsn, &env).unwrap_or_else(|_| panic!("{dsn}"));
        assert_eq!(pairs("host=h"), vec![tcp("h", 6001)]);
        assert_eq!(pairs("postgresql://u:p@h/d"), vec![tcp("h", 6001)]);
        assert_eq!(pairs("postgresql://u:p@h:5432/d"), vec![tcp("h", 5432)]);
        assert_eq!(pairs("host=h port=1"), vec![tcp("h", 1)]);
        // A URL with several hosts gives a port list (libpq): 5432 for each here.
        assert_eq!(
            pairs("postgresql://h1,h2/d"),
            vec![tcp("h1", 5432), tcp("h2", 5432)]
        );
        // The rest of the URL is kept.
        let cfg = config_env("postgresql://u:p@h/d?application_name=a", &env)
            .unwrap_or_else(|_| panic!("url"));
        assert_eq!(cfg.get_user(), Some("u"));
        assert_eq!(cfg.get_password(), Some(&b"p"[..]));
        assert_eq!(cfg.get_dbname(), Some("d"));
        assert_eq!(cfg.get_application_name(), Some("a"));
        for value in ["x", "70000", "1,x"] {
            match config_env("host=h", &[("PGPORT", value)]) {
                Err(QueryError::Interface(m)) => {
                    assert_eq!(m, format!("invalid PGPORT value {value:?}"));
                }
                _ => panic!("expected an InterfaceError for PGPORT={value:?}"),
            }
        }
    }
}
