//! The asyncio loop thread drives xitca-postgres itself.
//!
//! There is no I/O thread: the socket's readiness comes from asyncio (`loop.add_reader` /
//! `add_writer` callbacks), and xitca's own futures (its driver, the connect handshake, each
//! query) are polled on the loop thread by the tiny [`Executor`] below, whenever the loop
//! reports readiness or Python submits work. xitca still does all of the protocol work: it
//! only sees an `AsyncIo` implementation ([`LoopSocket`]) whose `poll_ready` is fed by those
//! callbacks instead of tokio's reactor.
//!
//! All polling happens with the GIL held, inside a Python call. Wakers normally fire on the
//! loop thread (from within a poll, or from `DriverTx::send` when a query is submitted); the
//! rare off-thread wake (a `__del__` on another thread) is forwarded through the
//! connection's mailbox socketpair, so the loop wakes up and runs the executor on the next
//! `drain()`. Nothing here needs a tokio runtime: no timers, no `tokio::spawn`, no
//! reactor, so fastpg runs no thread besides the loop's own.

use std::collections::VecDeque;
use std::future::poll_fn;
use std::io::{self, Read, Write};
use std::mem::ManuallyDrop;
use std::net::Shutdown;
use std::os::fd::{FromRawFd, RawFd};
use std::os::unix::net::UnixStream;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::task::{Context, Poll, Wake, Waker};

use xitca_io::io::{AsyncIo, Interest, Ready};

use crate::mailbox::Poker;

type Task = Pin<Box<dyn Future<Output = ()> + Send + 'static>>;

/// A number for the current thread, cheaper to read and compare than `thread::current().id()`
/// (and storable in an atomic; `ThreadId::as_u64` is unstable).
fn thread_key() -> u64 {
    static NEXT: AtomicU64 = AtomicU64::new(1);
    thread_local! {
        static KEY: u64 = NEXT.fetch_add(1, Ordering::Relaxed);
    }
    KEY.with(|key| *key)
}

/// Task ids woken since the last run, plus how to wake the loop from another thread.
struct ReadyQueue {
    queue: Mutex<VecDeque<usize>>,
    /// The `thread_key` of the thread running the loop: where the connection was created,
    /// then wherever the loop last called back (see `Executor::rehome`).
    loop_thread: AtomicU64,
    /// Wakes the loop through the connection's mailbox (see `mailbox.rs`).
    poke: Poker,
}

impl ReadyQueue {
    fn push(&self, id: usize) {
        self.queue
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push_back(id);
        if thread_key() != self.loop_thread.load(Ordering::Acquire) {
            // Off-thread wake: make the loop call `drain()`, which runs the executor.
            self.poke.poke();
        }
    }
}

struct TaskWaker {
    id: usize,
    /// Set while the id is queued; cleared when the task is polled (coalesces wakes).
    queued: AtomicBool,
    ready: Arc<ReadyQueue>,
}

impl Wake for TaskWaker {
    fn wake(self: Arc<Self>) {
        self.wake_by_ref();
    }

    fn wake_by_ref(self: &Arc<Self>) {
        if !self.queued.swap(true, Ordering::AcqRel) {
            self.ready.push(self.id);
        }
    }
}

struct Slot {
    task: Option<Task>,
    waker: Arc<TaskWaker>,
}

/// A single-threaded executor for `Send` futures, polled only from `run()`.
pub struct Executor {
    slots: Mutex<Vec<Option<Slot>>>,
    ready: Arc<ReadyQueue>,
    /// Guards against re-entrant runs (a nested Python call during `drain`'s decoding).
    running: AtomicBool,
}

impl Executor {
    /// Create it on the asyncio loop's thread; `poke` wakes the connection's mailbox.
    pub fn new(poke: Poker) -> Self {
        Self {
            slots: Mutex::new(Vec::new()),
            ready: Arc::new(ReadyQueue {
                queue: Mutex::new(VecDeque::new()),
                loop_thread: AtomicU64::new(thread_key()),
                poke,
            }),
            running: AtomicBool::new(false),
        }
    }

    pub fn on_loop_thread(&self) -> bool {
        thread_key() == self.ready.loop_thread.load(Ordering::Acquire)
    }

    /// Note that the loop runs on the current thread: call it from the loop's own callbacks
    /// (readiness, `call_soon`), which only ever run there. A loop may be run from another
    /// thread than the one it ran on when the connection was opened; without this, every
    /// poll would be left to a `drain()` that never polls (it would think itself off-thread),
    /// and the loop would spin on the mailbox.
    pub fn rehome(&self) {
        self.ready
            .loop_thread
            .store(thread_key(), Ordering::Release);
    }

    /// Whether a task was woken since the last `run()` (e.g. by a drop during `drain`).
    pub fn has_ready(&self) -> bool {
        !self
            .ready
            .queue
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .is_empty()
    }

    /// Queue `fut`; it is first polled by the next `run()`.
    pub fn spawn(&self, fut: impl Future<Output = ()> + Send + 'static) {
        let mut slots = self.slots.lock().unwrap_or_else(PoisonError::into_inner);
        let id = slots.iter().position(Option::is_none).unwrap_or_else(|| {
            slots.push(None);
            slots.len() - 1
        });
        let waker = Arc::new(TaskWaker {
            id,
            queued: AtomicBool::new(false),
            ready: self.ready.clone(),
        });
        slots[id] = Some(Slot {
            task: Some(Box::pin(fut)),
            waker: waker.clone(),
        });
        drop(slots);
        waker.wake_by_ref();
    }

    /// Poll every woken task until none is left woken. Must run on the loop thread.
    pub fn run(&self) {
        if self.running.swap(true, Ordering::AcqRel) {
            return;
        }
        // No tokio runtime context: tasks use no timers and spawn nothing (see
        // `bulk::wait_for_socket`).
        loop {
            let id = self
                .ready
                .queue
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .pop_front();
            let Some(id) = id else { break };
            // `get_mut`: a waker can outlive its slot (see `clear`).
            let taken = {
                let mut slots = self.slots.lock().unwrap_or_else(PoisonError::into_inner);
                slots.get_mut(id).and_then(Option::as_mut).and_then(|slot| {
                    slot.waker.queued.store(false, Ordering::Release);
                    slot.task.take().map(|task| (task, slot.waker.clone()))
                })
            };
            let Some((mut task, waker)) = taken else {
                continue;
            };
            let std_waker = Waker::from(waker);
            let done = task
                .as_mut()
                .poll(&mut Context::from_waker(&std_waker))
                .is_ready();
            let mut slots = self.slots.lock().unwrap_or_else(PoisonError::into_inner);
            if let Some(slot) = slots.get_mut(id) {
                if done {
                    *slot = None;
                } else if let Some(slot) = slot.as_mut() {
                    slot.task = Some(task);
                }
            }
        }
        self.running.store(false, Ordering::Release);
    }

    /// Drop every task unpolled: for a connection whose loop is gone, so nothing would poll
    /// them again (and they hold the connection's shared state, which holds them). Does
    /// nothing during a `run()`. The tasks are dropped after the lock is released, since
    /// dropping one releases Python objects, whose finalizers may spawn work.
    pub fn clear(&self) {
        if self.running.load(Ordering::Acquire) {
            return;
        }
        let tasks = std::mem::take(&mut *self.slots.lock().unwrap_or_else(PoisonError::into_inner));
        self.ready
            .queue
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clear();
        drop(tasks);
    }
}

#[derive(Default)]
struct Flags {
    readable: bool,
    /// Assumed until a write returns `WouldBlock`.
    writable: bool,
    /// A write would block: the socket should be registered with `loop.add_writer`.
    want_write: bool,
    read_waker: Option<Waker>,
    write_waker: Option<Waker>,
}

/// Readiness state shared between the socket and the asyncio callbacks.
#[derive(Default)]
pub struct IoState {
    flags: Mutex<Flags>,
}

impl IoState {
    fn lock(&self) -> std::sync::MutexGuard<'_, Flags> {
        self.flags.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// `loop.add_reader` callback.
    pub fn on_readable(&self) {
        let mut flags = self.lock();
        flags.readable = true;
        if let Some(w) = flags.read_waker.take() {
            w.wake();
        }
    }

    /// `loop.add_writer` callback.
    pub fn on_writable(&self) {
        let mut flags = self.lock();
        flags.writable = true;
        flags.want_write = false;
        if let Some(w) = flags.write_waker.take() {
            w.wake();
        }
    }

    /// Whether the socket currently needs a `loop.add_writer` registration.
    pub fn wants_writer(&self) -> bool {
        self.lock().want_write
    }
}

enum Sock {
    Tcp(ManuallyDrop<std::net::TcpStream>),
    Unix(ManuallyDrop<UnixStream>),
}

/// Size of [`LoopSocket`]'s read-ahead buffer.
const READ_AHEAD: usize = 16 * 1024;

/// An `AsyncIo` over a non-blocking socket owned by Python (the fd is never closed here;
/// the Python `socket` object does that after the loop has unregistered it).
///
/// Reads smaller than [`READ_AHEAD`] go through a buffer of that size: xitca reads into
/// buffers of about 4 KiB, so a large result took one `recv` per 4 KiB.
pub struct LoopSocket {
    sock: Sock,
    state: Arc<IoState>,
    /// Bytes read from the socket that the caller hasn't read yet: `ahead[pos..end]`
    /// (allocated on the first read).
    ahead: Box<[u8]>,
    pos: usize,
    end: usize,
}

impl LoopSocket {
    /// `fd` must stay open (and non-blocking) for as long as this value is used.
    pub fn new(fd: RawFd, unix: bool, state: Arc<IoState>) -> Self {
        // SAFETY: the caller guarantees `fd` is a live socket for the lifetime of this
        // value; `ManuallyDrop` keeps us from closing an fd we don't own.
        let sock = unsafe {
            if unix {
                Sock::Unix(ManuallyDrop::new(UnixStream::from_raw_fd(fd)))
            } else {
                Sock::Tcp(ManuallyDrop::new(std::net::TcpStream::from_raw_fd(fd)))
            }
        };
        let mut flags = state.lock();
        flags.writable = true;
        drop(flags);
        Self {
            sock,
            state,
            ahead: Box::default(),
            pos: 0,
            end: 0,
        }
    }

    /// Copy read-ahead bytes into `buf`.
    fn take_ahead(&mut self, buf: &mut [u8]) -> usize {
        let n = buf.len().min(self.end - self.pos);
        buf[..n].copy_from_slice(&self.ahead[self.pos..self.pos + n]);
        self.pos += n;
        n
    }
}

/// One `read` from the socket, noting when it drained the socket (see `LoopSocket::read`).
fn read_socket(sock: &mut Sock, state: &IoState, buf: &mut [u8]) -> io::Result<usize> {
    let result = match sock {
        Sock::Tcp(s) => s.read(buf),
        Sock::Unix(s) => s.read(buf),
    };
    let drained = match &result {
        // `Ok(0)` is EOF (or an empty `buf`): reads keep reporting it.
        Ok(n) => *n > 0 && *n < buf.len(),
        Err(e) => e.kind() == io::ErrorKind::WouldBlock,
    };
    if drained {
        state.lock().readable = false;
    }
    result
}

impl Read for LoopSocket {
    /// Reads until the socket is drained, as xitca does (it reads until `WouldBlock`), but
    /// without the last, empty `read` when it can tell: a read that doesn't fill `buf`
    /// took everything the kernel had, so the next one would return `WouldBlock`. That read
    /// is answered here instead, saving a syscall per response. The loop's reader is
    /// level-triggered, so data arriving after the short read is reported by the next
    /// `on_readable`, which sets `readable` again.
    ///
    /// A read smaller than [`READ_AHEAD`] reads up to that much from the socket and keeps
    /// the rest for the next reads (and for `poll_ready`, which reports it as readable).
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        if self.pos < self.end {
            return Ok(self.take_ahead(buf));
        }
        if !buf.is_empty() && !self.state.lock().readable {
            return Err(io::ErrorKind::WouldBlock.into());
        }
        if buf.is_empty() || buf.len() >= READ_AHEAD {
            return read_socket(&mut self.sock, &self.state, buf);
        }
        if self.ahead.is_empty() {
            self.ahead = vec![0; READ_AHEAD].into_boxed_slice();
        }
        self.end = read_socket(&mut self.sock, &self.state, &mut self.ahead)?;
        self.pos = 0;
        Ok(self.take_ahead(buf))
    }
}

impl Write for LoopSocket {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let result = match &mut self.sock {
            Sock::Tcp(s) => s.write(buf),
            Sock::Unix(s) => s.write(buf),
        };
        if let Err(e) = &result
            && e.kind() == io::ErrorKind::WouldBlock
        {
            let mut flags = self.state.lock();
            flags.writable = false;
            flags.want_write = true;
        }
        result
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl AsyncIo for LoopSocket {
    fn ready(&mut self, interest: Interest) -> impl Future<Output = io::Result<Ready>> + Send {
        poll_fn(move |cx| self.poll_ready(interest, cx))
    }

    fn poll_ready(&mut self, interest: Interest, cx: &mut Context<'_>) -> Poll<io::Result<Ready>> {
        let mut flags = self.state.lock();
        let mut ready = Ready::EMPTY;
        if interest.is_readable() && (flags.readable || self.pos < self.end) {
            ready |= Ready::READABLE;
        }
        if interest.is_writable() && flags.writable {
            ready |= Ready::WRITABLE;
        }
        if !ready.is_empty() {
            return Poll::Ready(Ok(ready));
        }
        if interest.is_readable() {
            flags.read_waker = Some(cx.waker().clone());
        }
        if interest.is_writable() {
            flags.write_waker = Some(cx.waker().clone());
        }
        Poll::Pending
    }

    fn is_vectored_write(&self) -> bool {
        false
    }

    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let result = match &self.get_mut().sock {
            Sock::Tcp(s) => s.shutdown(Shutdown::Write),
            Sock::Unix(s) => s.shutdown(Shutdown::Write),
        };
        // The peer may already be gone (ENOTCONN): nothing left to shut down.
        Poll::Ready(result.or_else(|e| match e.kind() {
            io::ErrorKind::NotConnected => Ok(()),
            _ => Err(e),
        }))
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::AtomicUsize;
    use std::thread;

    use super::*;

    #[test]
    fn executor_runs_tasks_to_completion_and_reuses_slots() {
        let mailbox = crate::mailbox::Mailbox::<()>::new().unwrap();
        let exec = Executor::new(mailbox.poker().unwrap());
        let count = Arc::new(AtomicUsize::new(0));
        for _ in 0..3 {
            let count = count.clone();
            exec.spawn(async move {
                // A self-wake: the task is polled twice before finishing.
                let mut yielded = false;
                poll_fn(|cx| {
                    if yielded {
                        return Poll::Ready(());
                    }
                    yielded = true;
                    cx.waker().wake_by_ref();
                    Poll::Pending
                })
                .await;
                count.fetch_add(1, Ordering::SeqCst);
            });
        }
        exec.run();
        assert_eq!(count.load(Ordering::SeqCst), 3);
        assert!(exec.slots.lock().unwrap().iter().all(Option::is_none));
    }

    /// A socket pair: our end as a `LoopSocket` (reported readable), and the peer.
    fn socket_pair() -> (LoopSocket, UnixStream, UnixStream) {
        use std::os::fd::AsRawFd;
        let (ours, peer) = UnixStream::pair().unwrap();
        ours.set_nonblocking(true).unwrap();
        let state = Arc::new(IoState::default());
        state.on_readable();
        // `ours` is returned, so its fd stays open for as long as the `LoopSocket` uses it.
        (LoopSocket::new(ours.as_raw_fd(), true, state), ours, peer)
    }

    fn poll_readable(socket: &mut LoopSocket) -> bool {
        let mut cx = Context::from_waker(Waker::noop());
        matches!(
            socket.poll_ready(Interest::READABLE, &mut cx),
            Poll::Ready(Ok(ready)) if ready.is_readable()
        )
    }

    #[test]
    fn read_ahead_serves_small_reads_and_stays_readable() {
        let (mut socket, _ours, mut peer) = socket_pair();
        let sent: Vec<u8> = (0..100u8).collect();
        peer.write_all(&sent).unwrap();
        let mut byte = [0u8; 1];
        assert_eq!(socket.read(&mut byte).unwrap(), 1);
        assert_eq!(byte, [0]);
        // The socket is drained (a short read), but 99 bytes are kept: still readable.
        assert!(poll_readable(&mut socket));
        let mut rest = [0u8; 200];
        assert_eq!(socket.read(&mut rest).unwrap(), 99);
        assert_eq!(&rest[..99], &sent[1..]);
        // Nothing kept and the socket drained: no syscall, not readable.
        assert_eq!(
            socket.read(&mut rest).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        assert!(!poll_readable(&mut socket));
    }

    #[test]
    fn read_ahead_keeps_order_across_refills_and_reports_eof() {
        let (mut socket, _ours, mut peer) = socket_pair();
        // More than two read-ahead buffers' worth, read 4 KiB at a time as xitca does.
        let sent: Vec<u8> = (0..READ_AHEAD * 2 + 123).map(|i| (i % 251) as u8).collect();
        let writer = thread::spawn(move || peer.write_all(&sent).map(|()| sent));
        let mut got = Vec::new();
        let mut buf = [0u8; 4096];
        loop {
            match socket.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => got.extend_from_slice(&buf[..n]),
                // What the loop's reader callback would do once more data arrives.
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    thread::yield_now();
                    socket.state.on_readable();
                }
                Err(e) => panic!("{e}"),
            }
        }
        assert_eq!(got, writer.join().unwrap().unwrap());
    }

    #[test]
    fn off_thread_wake_pokes_the_mailbox() {
        let mailbox = crate::mailbox::Mailbox::<()>::new().unwrap();
        let exec = Executor::new(mailbox.poker().unwrap());
        let (tx, rx_watch) = tokio::sync::watch::channel(false);
        let mut rx_watch = rx_watch;
        exec.spawn(async move {
            let _ = rx_watch.wait_for(|v| *v).await;
        });
        exec.run(); // registers the waker
        thread::spawn(move || tx.send_replace(true)).join().unwrap();
        // The wake-up byte sits in the mailbox: the loop would now call `drain()`.
        let mut sink = Vec::new();
        mailbox.take_into(&mut sink);
        exec.run();
        assert!(exec.slots.lock().unwrap().iter().all(Option::is_none));
    }

    #[test]
    fn rehome_moves_the_loop_thread() {
        let mailbox = crate::mailbox::Mailbox::<()>::new().unwrap();
        let exec = Arc::new(Executor::new(mailbox.poker().unwrap()));
        assert!(exec.on_loop_thread());
        let moved = exec.clone();
        let (elsewhere, rehomed) = thread::spawn(move || {
            let elsewhere = moved.on_loop_thread();
            moved.rehome();
            (elsewhere, moved.on_loop_thread())
        })
        .join()
        .unwrap();
        assert!(!elsewhere);
        assert!(rehomed);
        assert!(!exec.on_loop_thread());
    }
}
