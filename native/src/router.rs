//! Routing state for the pool's shared statements (`fastpg._pool.Pool`).
//!
//! Per pooled connection (a *slot*): the shared statements in flight, oldest first, with
//! their send times and expected service times, and when its last one completed. Plus the
//! learned service time of each SQL text. The pool calls `begin` when it sends a statement,
//! `finish` when one completes and, when no connection is idle, `best` to pick the least
//! stalled one. The rest of routing (the idle stack, states, waiters) stays in Python.
//!
//! The rules are `_pool.py`'s: a slot's score is `max(expected work queued, age of its
//! oldest statement)`; a statement's expected work is an EWMA (α = 1/4) of its SQL text's
//! service times, each measured from `max(sent, previous completion on the slot)`; SQL not
//! seen before is expected to take a default time.
//!
//! In Python this bookkeeping was a few dozen bytecodes per statement (~400 ns) and the
//! scoring loop ~120 ns per connection; here it is two or three calls of ~30 ns each, plus
//! a loop of a few ns per connection (`notes/perf-pool.md`). Times are nanoseconds since
//! the router was created, on a clock that keeps counting while the machine sleeps.

use std::collections::{HashMap, VecDeque};

use pyo3::exceptions::PyKeyError;
use pyo3::prelude::*;
use pyo3::types::PyString;

/// Nanoseconds on a monotonic clock that counts time asleep. `Instant`'s doesn't (it is
/// `CLOCK_UPTIME_RAW` on macOS, `CLOCK_MONOTONIC` on Linux), so a laptop asleep all night
/// would wake with its connections only seconds idle, though the server or the network has
/// long since dropped them: `max_idle` wouldn't close them.
fn clock_ns() -> i64 {
    #[cfg(target_vendor = "apple")]
    const CLOCK: libc::clockid_t = libc::CLOCK_MONOTONIC;
    #[cfg(not(target_vendor = "apple"))]
    const CLOCK: libc::clockid_t = libc::CLOCK_BOOTTIME;
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: `ts` is a valid timespec to write to, and `CLOCK` exists on this platform.
    unsafe { libc::clock_gettime(CLOCK, &raw mut ts) };
    ts.tv_sec * 1_000_000_000 + ts.tv_nsec
}

/// `old + (service - old) // 4`, with Python's floor division.
fn ewma(old: i64, service: i64) -> i64 {
    old + (service - old).div_euclid(4)
}

struct InFlight {
    token: u64,
    sent: i64,
    cost: i64,
}

#[derive(Default)]
struct Slot {
    started: VecDeque<InFlight>,
    expected: i64,
    done: i64,
    shared: bool,
}

/// The per-slot bookkeeping, apart from Python (so it is unit-tested on its own).
#[derive(Default)]
struct Slots {
    slots: Vec<Slot>,
    free: Vec<usize>,
    token: u64,
}

impl Slots {
    fn get(&self, slot: usize) -> PyResult<&Slot> {
        self.slots
            .get(slot)
            .ok_or_else(|| PyKeyError::new_err(slot))
    }

    fn get_mut(&mut self, slot: usize) -> PyResult<&mut Slot> {
        self.slots
            .get_mut(slot)
            .ok_or_else(|| PyKeyError::new_err(slot))
    }

    fn add(&mut self, now: i64) -> usize {
        let slot = Slot {
            done: now,
            shared: true,
            ..Slot::default()
        };
        if let Some(i) = self.free.pop() {
            self.slots[i] = slot;
            i
        } else {
            self.slots.push(slot);
            self.slots.len() - 1
        }
    }

    fn remove(&mut self, slot: usize) -> PyResult<()> {
        *self.get_mut(slot)? = Slot::default();
        self.free.push(slot);
        Ok(())
    }

    fn begin(&mut self, slot: usize, cost: i64, now: i64) -> PyResult<u64> {
        let s = self
            .slots
            .get_mut(slot)
            .ok_or_else(|| PyKeyError::new_err(slot))?;
        self.token += 1;
        s.started.push_back(InFlight {
            token: self.token,
            sent: now,
            cost,
        });
        s.expected += cost;
        Ok(self.token)
    }

    /// The statement's service time, and how many are left in flight on the slot.
    fn finish(&mut self, slot: usize, token: u64, now: i64) -> PyResult<(i64, usize)> {
        let s = self.get_mut(slot)?;
        // Statements mostly complete in send order, so this is usually the first one.
        let i = s
            .started
            .iter()
            .position(|f| f.token == token)
            .ok_or_else(|| PyKeyError::new_err(token))?;
        let Some(f) = s.started.remove(i) else {
            unreachable!("position() found it");
        };
        s.expected -= f.cost;
        // On a pipelined connection a statement only starts once the one before it is done.
        let service = now - f.sent.max(s.done);
        s.done = now;
        Ok((service, s.started.len()))
    }

    fn best(&self, cap: usize, now: i64) -> Option<usize> {
        let mut best = None;
        let mut best_score = i64::MAX;
        for (i, s) in self.slots.iter().enumerate() {
            let n = s.started.len();
            if !s.shared || n == 0 || n >= cap {
                continue;
            }
            // The queue's expected work, or how long it has been busy if that's longer (a
            // statement overrunning its estimate is assumed to keep running). Not
            // `expected - age`: that rates a connection "almost done" with a 20 ms statement
            // as free, and measured p99 got worse when estimates overran.
            let score = s.expected.max(now - s.started[0].sent);
            if score < best_score {
                best = Some(i);
                best_score = score;
            }
        }
        best
    }
}

/// SQL text -> learned service time, keyed by the str's (cached) Python hash. The texts
/// are kept to tell colliding hashes apart, compared by identity first: callers reuse one
/// str object per statement.
type Costs = HashMap<isize, Vec<(Py<PyString>, i64)>>;

fn same(text: &Py<PyString>, sql: &Bound<'_, PyString>) -> PyResult<bool> {
    Ok(text.as_ptr() == sql.as_ptr() || text.bind(sql.py()).as_any().eq(sql)?)
}

/// The pool's routing state. The pool calls it from its event loop's thread only; it is
/// not `unsendable`, so a pool garbage-collected on another thread still drops it cleanly
/// (PyO3's borrow flag turns any concurrent use into an error).
#[pyclass(module = "fastpg._native")]
pub struct Router {
    epoch: i64,
    slots: Slots,
    costs: Costs,
    n_costs: usize,
    default_cost: i64,
    max_costs: usize,
}

impl Router {
    fn now(&self) -> i64 {
        clock_ns() - self.epoch
    }

    fn cost(&self, sql: &Bound<'_, PyString>) -> PyResult<i64> {
        if let Some(bucket) = self.costs.get(&sql.hash()?) {
            for (text, cost) in bucket {
                if same(text, sql)? {
                    return Ok(*cost);
                }
            }
        }
        Ok(self.default_cost)
    }

    fn learn(&mut self, sql: &Bound<'_, PyString>, service: i64) -> PyResult<()> {
        let hash = sql.hash()?;
        if let Some(bucket) = self.costs.get_mut(&hash) {
            for (text, cost) in bucket.iter_mut() {
                if same(text, sql)? {
                    *cost = ewma(*cost, service);
                    return Ok(());
                }
            }
        }
        if self.n_costs >= self.max_costs {
            self.costs.clear(); // e.g. generated SQL: start over rather than grow forever
            self.n_costs = 0;
        }
        self.costs
            .entry(hash)
            .or_default()
            .push((sql.clone().unbind(), service));
        self.n_costs += 1;
        Ok(())
    }
}

#[pymethods]
impl Router {
    #[new]
    fn new(default_cost: i64, max_costs: usize) -> Self {
        Self {
            epoch: clock_ns(),
            slots: Slots::default(),
            costs: HashMap::new(),
            n_costs: 0,
            default_cost,
            max_costs,
        }
    }

    /// A new slot: shared, nothing in flight, idle from now.
    fn add(&mut self) -> usize {
        let now = self.now();
        self.slots.add(now)
    }

    /// Forget a slot (its connection left the pool and nothing is in flight on it).
    fn remove(&mut self, slot: usize) -> PyResult<()> {
        self.slots.remove(slot)
    }

    /// Whether `best` may pick the slot (its connection is in shared rotation).
    fn set_shared(&mut self, slot: usize, shared: bool) -> PyResult<()> {
        self.slots.get_mut(slot)?.shared = shared;
        Ok(())
    }

    /// Mark the slot idle from now (its connection rejoined shared rotation).
    fn touch(&mut self, slot: usize) -> PyResult<()> {
        let now = self.now();
        self.slots.get_mut(slot)?.done = now;
        Ok(())
    }

    /// Nanoseconds since the slot's last statement completed (or since `add` / `touch`).
    fn idle_ns(&self, slot: usize) -> PyResult<i64> {
        Ok(self.now() - self.slots.get(slot)?.done)
    }

    /// Statements in flight on the slot.
    fn load(&self, slot: usize) -> PyResult<usize> {
        Ok(self.slots.get(slot)?.started.len())
    }

    /// Record a statement sent on the slot now; returns its token.
    fn begin(&mut self, slot: usize, query: &Bound<'_, PyString>) -> PyResult<u64> {
        let cost = self.cost(query)?;
        let now = self.now();
        self.slots.begin(slot, cost, now)
    }

    /// Record a statement's completion; returns the statements still in flight on the slot.
    /// Pass `query` on success only: its service time then feeds the estimate.
    #[pyo3(signature = (slot, token, query=None))]
    fn finish(
        &mut self,
        slot: usize,
        token: u64,
        query: Option<&Bound<'_, PyString>>,
    ) -> PyResult<usize> {
        let now = self.now();
        let (service, left) = self.slots.finish(slot, token, now)?;
        if let Some(query) = query {
            self.learn(query, service)?;
        }
        Ok(left)
    }

    /// The shared slot with the lowest stall score among those with statements in flight
    /// but fewer than `cap`, or None.
    fn best(&self, cap: usize) -> Option<usize> {
        self.slots.best(cap, self.now())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn load(slots: &Slots, slot: usize) -> usize {
        slots.slots[slot].started.len()
    }

    #[test]
    fn ewma_floors_like_python() {
        assert_eq!(ewma(100, 200), 125);
        assert_eq!(ewma(100, 0), 75);
        assert_eq!(ewma(10, 7), 9); // 10 + (-3) // 4 == 9 in Python
    }

    #[test]
    fn service_time_starts_after_the_previous_completion() {
        let mut slots = Slots::default();
        let s = slots.add(0);
        let a = slots.begin(s, 50, 100).unwrap();
        let b = slots.begin(s, 50, 110).unwrap();
        assert_eq!(slots.finish(s, a, 200).unwrap(), (100, 1));
        // b was queued behind a: it ran from 200, not from when it was sent.
        assert_eq!(slots.finish(s, b, 260).unwrap(), (60, 0));
        assert_eq!(slots.slots[s].expected, 0);
    }

    #[test]
    fn best_is_the_lowest_score_under_the_cap() {
        let mut slots = Slots::default();
        let (x, y, z) = (slots.add(0), slots.add(0), slots.add(0));
        slots.begin(x, 1_000, 0).unwrap(); // at t=500: expected 1000, age 500
        slots.begin(y, 10, 100).unwrap(); // at t=500: expected 10, age 400
        assert_eq!(slots.best(8, 500), Some(y));
        assert_eq!(slots.best(1, 500), None); // both at the cap; z is idle, not scored
        assert_eq!(load(&slots, z), 0);
        slots.slots[y].shared = false;
        assert_eq!(slots.best(8, 500), Some(x));
    }

    #[test]
    fn age_moves_to_the_next_statement_when_the_oldest_completes() {
        let mut slots = Slots::default();
        let (x, y) = (slots.add(0), slots.add(0));
        let first = slots.begin(x, 1, 0).unwrap();
        slots.begin(x, 1, 900).unwrap();
        slots.begin(y, 1, 500).unwrap();
        assert_eq!(slots.best(8, 1_000), Some(y)); // ages: x 1000, y 500
        slots.finish(x, first, 1_000).unwrap();
        assert_eq!(slots.best(8, 1_000), Some(x)); // ages: x 100, y 500
    }

    #[test]
    fn removed_slots_are_reused_clean() {
        let mut slots = Slots::default();
        let x = slots.add(0);
        slots.begin(x, 5, 0).unwrap();
        slots.remove(x).unwrap();
        let y = slots.add(7);
        assert_eq!(y, x);
        assert_eq!(load(&slots, y), 0);
        assert_eq!(slots.slots[y].expected, 0);
        assert_eq!(slots.slots[y].done, 7);
        assert!(slots.slots[y].shared);
    }
}
