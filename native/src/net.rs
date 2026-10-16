//! Connection setup with server-close detection.
//!
//! xitca-postgres 0.4's driver, on reading EOF, only marks its read half closed and then
//! waits for the client to write again. A connection closed by the server (restart,
//! `pg_terminate_backend`, idle timeout) is therefore never noticed, and a query sent
//! afterwards can wait forever for a response. Python connects the socket (to the hosts
//! [`targets`] lists), and we wrap it in [`EofWatch`] (which signals when a read returns EOF)
//! before handing it to `Postgres::connect_io`; the connection task then drops the driver on
//! EOF, which fails all pending requests promptly.
//!
//! TLS (`tls.rs`) sits between the socket and the watch: `EofWatch<TlsStream<socket>>`, so
//! the watch sees plaintext (a `close_notify` is a 0-byte read, a TLS error a fatal one).
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
use std::sync::{Arc, Mutex, PoisonError};
use std::task::{Context, Poll};

use tokio::sync::Notify;
use xitca_io::io::{AsyncIo, Interest, Ready};
use xitca_postgres::{Client, Config, Driver, Postgres};

use crate::auth;
use crate::errors::QueryError;
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
/// that for backpressure, since xitca's write buffer is not observable), and the response
/// cycles a bulk write collects.
#[derive(Default)]
pub struct IoWatch {
    pub eof: Notify,
    /// Wakes waiters after every successful write.
    pub progress: Notify,
    written: AtomicU64,
    collecting: AtomicBool,
    tap: Mutex<Tap>,
}

impl IoWatch {
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

/// Follows the message framing of everything read from the server (always: it is cheap, a
/// header per message, and so it is at a message boundary whenever a bulk write starts
/// collecting), and reports to [`IoWatch::record`] the complete messages of the cycles that
/// began while it collects.
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
            if self.reporting && self.header[0] == b'C' {
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
        if tag == b'Z' {
            self.first = None;
            self.began_collecting = false;
        }
    }
}

/// Transparent `AsyncIo` wrapper that fires `eof` when the peer closes the stream, counts
/// written bytes and follows the messages read (for [`IoWatch::take_cycle`]).
pub struct EofWatch<Io> {
    io: Io,
    eof: Arc<IoWatch>,
    framer: Framer,
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
}

impl<Io: io::Read> io::Read for EofWatch<Io> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
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
                self.eof.wrote(n);
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
                self.eof.wrote(n);
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
        if let Err(e) = &result
            && is_fatal(e)
        {
            self.signal_eof();
        }
        result
    }
}

impl<Io: AsyncIo + Send> AsyncIo for EofWatch<Io> {
    fn ready(&mut self, interest: Interest) -> impl Future<Output = io::Result<Ready>> + Send {
        self.io.ready(interest)
    }

    fn poll_ready(&mut self, interest: Interest, cx: &mut Context<'_>) -> Poll<io::Result<Ready>> {
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

/// Parse `dsn` into xitca's config and the settings xitca never sees (TLS, socket options).
pub fn config(dsn: &str) -> Result<(Config, DsnOptions), QueryError> {
    let (rest, options) = tls::split_dsn(dsn)?;
    // Every key is known by now (`split_dsn` names an unknown one), and xitca's parse errors
    // carry no detail at all ("WIP error type ...").
    let cfg = Config::try_from(rest.as_ref()).map_err(|_| {
        QueryError::Interface(
            "invalid connection string: malformed, or an invalid port or \
             target_session_attrs value"
                .to_owned(),
        )
    })?;
    Ok((cfg, options))
}

/// libpq's socket keys of `dsn` (after checking the whole DSN).
pub fn socket_options(dsn: &str) -> Result<SocketOptions, QueryError> {
    Ok(config(dsn)?.1.socket)
}

/// The hosts of `dsn` as `("tcp" | "unix", host or directory/path, port)`, in connection
/// order, for Python to connect the socket to.
pub fn targets(dsn: &str) -> Result<Vec<(&'static str, String, u16)>, QueryError> {
    let (cfg, _) = config(dsn)?;
    let mut out = Vec::new();
    for host in cfg.get_hosts() {
        let target = parse_host(&format!("{host:?}"))
            .ok_or_else(|| QueryError::Interface(format!("unsupported host {host:?}")))?;
        for &port in cfg.get_ports() {
            out.push(match &target {
                Target::Tcp(name) => ("tcp", name.clone(), port),
                Target::Unix(path) => ("unix", path.to_string_lossy().into_owned(), port),
            });
        }
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
        return startup(io, cfg).await;
    }
    let io = auth::authenticate(io, &cfg, binding, encrypted).await?;
    startup(io, cfg).await
}

async fn startup<Io: AsyncIo + Send + 'static>(
    io: Io,
    cfg: Config,
) -> Result<(Client, Driver, Arc<IoWatch>), QueryError> {
    let eof = Arc::new(IoWatch::default());
    let watched = EofWatch {
        io,
        eof: eof.clone(),
        framer: Framer::default(),
    };
    let (client, driver) = Postgres::new(cfg).connect_io(watched).await?;
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
}
