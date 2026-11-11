//! LISTEN/NOTIFY: the notifications a connection's driver receives, queued for its listeners.
//!
//! The driver task (`connection::serve`) hands every NotificationResponse to [`deliver`],
//! which queues it in the [`Inbox`] of each listener of its channel, and wakes the one waiting
//! for it the way a query's result does: a completion posted to the connection's mailbox
//! resolves the listener's future. Python (`fastpg._listen`) then takes the notifications
//! with [`Listener::pop`]. Python sends LISTEN and UNLISTEN; Rust only keeps the queues.
//!
//! An inbox holds at most `max_queued` notifications. One more and it stops queueing for good:
//! [`Listener::pop`] raises `ListenerOverflowError` from then on, so nothing is lost silently.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex, PoisonError};

use postgres_protocol::message::backend::NotificationResponseBody;
use pyo3::prelude::*;

use crate::connection::{Outcome, Shared, plain_request};
use crate::errors::py_err;

/// One notification: the sending backend's process id, the channel and the payload.
struct Note {
    pid: i32,
    channel: String,
    payload: String,
}

#[derive(Default)]
struct InboxState {
    queue: VecDeque<Note>,
    /// It filled up: nothing is queued any more, and `pop` raises.
    overflowed: bool,
    /// Its `async with` block ended (`close()`): `pop` raises.
    closed: bool,
    /// The listener's pending `wait()`, resolved when something arrives or the connection
    /// ends.
    waiter: Option<Py<PyAny>>,
}

/// One listener's channels and queue (registered in `Shared::listeners`).
pub(crate) struct Inbox {
    channels: Vec<String>,
    max_queued: usize,
    state: Mutex<InboxState>,
}

impl Inbox {
    fn state(&self) -> std::sync::MutexGuard<'_, InboxState> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }
}

/// Queue a notification for every listener of its channel; on the loop thread, from the
/// driver task. Notifications on channels nobody listens to are dropped.
pub(crate) fn deliver(shared: &Shared, body: &NotificationResponseBody) {
    let listeners = shared
        .listeners
        .lock()
        .unwrap_or_else(PoisonError::into_inner);
    let (Ok(channel), Ok(payload)) = (body.channel(), body.message()) else {
        return; // not UTF-8: can't happen with the UTF8 client_encoding fastpg sets
    };
    for inbox in listeners
        .iter()
        .filter(|inbox| inbox.channels.iter().any(|c| c == channel))
    {
        let mut state = inbox.state();
        if state.overflowed {
            continue;
        }
        if state.queue.len() < inbox.max_queued {
            state.queue.push_back(Note {
                pid: body.process_id(),
                channel: channel.to_owned(),
                payload: payload.to_owned(),
            });
        } else {
            state.overflowed = true;
        }
        if let Some(waiter) = state.waiter.take() {
            shared.post(plain_request(waiter), Outcome::Done);
        }
    }
}

/// The connection is gone: wake every waiting listener (their `pop` then raises once their
/// queue is empty). On the loop thread, from the driver task, once `dead` is set.
pub(crate) fn wake_all(shared: &Shared) {
    let listeners = shared
        .listeners
        .lock()
        .unwrap_or_else(PoisonError::into_inner);
    for inbox in listeners.iter() {
        if let Some(waiter) = inbox.state().waiter.take() {
            shared.post(plain_request(waiter), Outcome::Done);
        }
    }
}

/// Register a listener of `channels` on the connection.
pub(crate) fn register(shared: Arc<Shared>, channels: Vec<String>, max_queued: usize) -> Listener {
    let inbox = Arc::new(Inbox {
        channels,
        max_queued,
        state: Mutex::default(),
    });
    shared
        .listeners
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
        .push(inbox.clone());
    Listener { shared, inbox }
}

/// A listener's queue; see `fastpg._listen.Listener`.
#[pyclass(frozen, module = "fastpg._native")]
pub struct Listener {
    shared: Arc<Shared>,
    inbox: Arc<Inbox>,
}

#[pymethods]
impl Listener {
    /// The next notification as `(channel, payload, pid)`, or None if none is queued.
    /// Raises `InterfaceError` once closed, `ListenerOverflowError` once the queue has
    /// overflowed, and `ConnectionClosedError` once the connection is gone and the queue
    /// empty.
    fn pop(&self, py: Python<'_>) -> PyResult<Option<(String, String, i32)>> {
        let mut state = self.inbox.state();
        if state.closed {
            return Err(py_err(
                py,
                "InterfaceError",
                "the listener was closed when its listen() block ended",
            ));
        }
        if state.overflowed {
            let message = format!(
                "the listener had {} notifications queued (max_queued) when more arrived: \
                 notifications were lost",
                self.inbox.max_queued
            );
            return Err(py_err(py, "ListenerOverflowError", message));
        }
        if let Some(note) = state.queue.pop_front() {
            return Ok(Some((note.channel, note.payload, note.pid)));
        }
        if self.shared.dead() {
            return Err(self.shared.closed_error().into_pyerr(py));
        }
        Ok(None)
    }

    /// Resolve `future` (to None) when a notification arrives or the connection ends.
    fn wait(&self, py: Python<'_>, future: Py<PyAny>) -> PyResult<()> {
        let mut state = self.inbox.state();
        if state.closed || state.overflowed || !state.queue.is_empty() || self.shared.dead() {
            drop(state);
            future.bind(py).call_method1("set_result", (py.None(),))?;
            return Ok(());
        }
        state.waiter = Some(future);
        Ok(())
    }

    /// Stop queueing, and refuse `pop` from now on (idempotent); a task still waiting (in
    /// another task than the block's) wakes up to that refusal. Returns the channels no
    /// other listener on the connection still listens to: the ones to UNLISTEN.
    fn close(&self, py: Python<'_>) -> Vec<String> {
        let waiter = {
            let mut state = self.inbox.state();
            state.closed = true;
            state.queue.clear();
            state.waiter.take()
        };
        if let Some(waiter) = waiter {
            // Already done if its awaiter was cancelled: nobody to wake then.
            let _ = waiter.bind(py).call_method1("set_result", (py.None(),));
        }
        let mut listeners = self
            .shared
            .listeners
            .lock()
            .unwrap_or_else(PoisonError::into_inner);
        let Some(index) = listeners
            .iter()
            .position(|inbox| Arc::ptr_eq(inbox, &self.inbox))
        else {
            return Vec::new();
        };
        listeners.swap_remove(index);
        self.inbox
            .channels
            .iter()
            .filter(|channel| {
                !listeners
                    .iter()
                    .any(|inbox| inbox.channels.contains(channel))
            })
            .cloned()
            .collect()
    }
}
