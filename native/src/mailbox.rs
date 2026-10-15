//! A connection's completion queue, plus a socketpair that wakes the asyncio loop from
//! another thread.
//!
//! Completions are queued by the connection's tasks, which only run on the loop thread, and
//! taken right after by the same thread (`Shared::pump`), so queueing needs no wake-up. The
//! socketpair is for the rare task woken off the loop thread (see `event_loop.rs`): a
//! [`Poker`] writes one byte, only when the mailbox goes from "idle" to "armed", which makes
//! the loop (`loop.add_reader` on [`Mailbox::fileno`]) pump the connection; there
//! [`Mailbox::take_into`] reads the bytes and disarms. A burst of wakes costs one byte.

use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, RawFd};
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};

pub struct Mailbox<T> {
    items: Mutex<Vec<T>>,
    armed: Arc<AtomicBool>,
    tx: UnixStream,
    rx: UnixStream,
}

/// Wakes a mailbox's consumer from another thread (`event_loop.rs` uses it for task wakes
/// that happen off the loop thread).
pub struct Poker {
    tx: UnixStream,
    armed: Arc<AtomicBool>,
}

impl Poker {
    pub fn poke(&self) {
        if !self.armed.swap(true, Ordering::SeqCst) {
            let _ = (&self.tx).write(&[1]);
        }
    }
}

impl<T> Mailbox<T> {
    pub fn new() -> io::Result<Self> {
        let (tx, rx) = UnixStream::pair()?;
        tx.set_nonblocking(true)?;
        rx.set_nonblocking(true)?;
        Ok(Self {
            items: Mutex::new(Vec::new()),
            armed: Arc::new(AtomicBool::new(false)),
            tx,
            rx,
        })
    }

    /// File descriptor to register with `loop.add_reader`.
    pub fn fileno(&self) -> RawFd {
        self.rx.as_raw_fd()
    }

    pub fn poker(&self) -> io::Result<Poker> {
        Ok(Poker {
            tx: self.tx.try_clone()?,
            armed: self.armed.clone(),
        })
    }

    /// Queue an item without a wake-up: for producers on the consumer's own thread, which
    /// call `take_into` themselves right after.
    pub fn push(&self, item: T) {
        self.items
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .push(item);
    }

    /// Move every queued item into `out` (which should be empty).
    ///
    /// Clears a pending wake-up first. A poke racing with that may leave no byte behind,
    /// which loses nothing: the executor queues the woken task before poking, and
    /// `Shared::pump` looks for woken tasks again after taking the items. The socket is only
    /// read when armed, so taking items makes no syscall unless something poked.
    pub fn take_into(&self, out: &mut Vec<T>) {
        if self.armed.load(Ordering::SeqCst) {
            let mut sink = [0u8; 64];
            while let Ok(n) = (&self.rx).read(&mut sink) {
                if n < sink.len() {
                    break;
                }
            }
            self.armed.store(false, Ordering::SeqCst);
        }
        std::mem::swap(
            &mut *self.items.lock().unwrap_or_else(PoisonError::into_inner),
            out,
        );
    }
}
