//! Per-connection LRU cache of server-side prepared statements, keyed by SQL text.
//!
//! Each entry also memoises what depends only on the statement's result columns:
//! the column codecs for tuple rows and the column→field bindings per Struct type.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock, PoisonError};

use pyo3::prelude::*;
use xitca_postgres::{Client, Statement};

use crate::decode::Codec;
use crate::structs::{Binding, StructSpec};

pub struct CachedStmt {
    sql: Box<str>,
    stmt: Option<Statement>,
    client: Arc<Client>,
    codecs: OnceLock<Result<Arc<[Codec]>, String>>,
    bindings: Mutex<Vec<(Py<StructSpec>, Arc<Binding>)>>,
    /// The connection is closing: the session's end frees the server-side statement, so
    /// dropping it sends no Close (whose answer nobody would read once the socket closes).
    session_ending: AtomicBool,
}

impl CachedStmt {
    pub fn new(sql: &str, stmt: Statement, client: Arc<Client>) -> Self {
        Self {
            sql: sql.into(),
            stmt: Some(stmt),
            client,
            codecs: OnceLock::new(),
            bindings: Mutex::new(Vec::new()),
            session_ending: AtomicBool::new(false),
        }
    }

    pub fn sql(&self) -> &str {
        &self.sql
    }

    // `stmt` is `Some` from `new` until `Drop` takes it, and nothing else can see it then.
    #[allow(clippy::expect_used)]
    pub fn statement(&self) -> &Statement {
        self.stmt.as_ref().expect("statement is only taken on drop")
    }

    /// Codec per result column (for tuple rows and the msgpack path).
    pub fn codecs(&self) -> Result<Arc<[Codec]>, String> {
        self.codecs
            .get_or_init(|| {
                self.statement()
                    .columns()
                    .iter()
                    .map(|c| {
                        Codec::for_type(c.r#type()).ok_or_else(|| {
                            format!(
                                "column {:?} has PostgreSQL type {}, which fastpg cannot decode",
                                c.name(),
                                c.r#type().name()
                            )
                        })
                    })
                    .collect()
            })
            .clone()
    }

    /// Column→field binding for `spec`, built (and type-checked) on first use.
    pub fn binding(&self, py: Python<'_>, spec: &Bound<'_, StructSpec>) -> PyResult<Arc<Binding>> {
        let mut bindings = self.bindings.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some((_, b)) = bindings.iter().find(|(s, _)| s.as_ptr() == spec.as_ptr()) {
            return Ok(b.clone());
        }
        let binding = Arc::new(Binding::build(py, spec.get(), self.statement().columns())?);
        bindings.push((spec.clone().unbind(), binding.clone()));
        Ok(binding)
    }
}

impl Drop for CachedStmt {
    fn drop(&mut self) {
        // Deallocate the server-side statement (no-op if the connection is already gone).
        if let Some(stmt) = self.stmt.take()
            && !self.session_ending.load(Ordering::Acquire)
        {
            drop(stmt.into_guarded(&*self.client));
        }
    }
}

struct Entry {
    stmt: Arc<CachedStmt>,
    last_used: u64,
}

pub struct StatementCache {
    capacity: usize,
    /// Entries, the LRU clock, and whether the cache is closed (see [`Self::close`]).
    inner: Mutex<(HashMap<Box<str>, Entry>, u64, bool)>,
}

impl StatementCache {
    pub fn new(capacity: usize) -> Self {
        Self {
            capacity: capacity.max(1),
            inner: Mutex::new((HashMap::new(), 0, false)),
        }
    }

    pub fn get(&self, sql: &str) -> Option<Arc<CachedStmt>> {
        let mut guard = self.inner.lock().unwrap_or_else(PoisonError::into_inner);
        let (map, clock, _) = &mut *guard;
        *clock += 1;
        map.get_mut(sql).map(|e| {
            e.last_used = *clock;
            e.stmt.clone()
        })
    }

    /// Insert a freshly prepared statement; if a concurrent task won the race, return theirs
    /// (ours is dropped and deallocated on the server). A closed cache keeps nothing: the
    /// statement is returned for its one use.
    pub fn insert(&self, sql: String, stmt: Arc<CachedStmt>) -> Arc<CachedStmt> {
        let evicted;
        let result;
        {
            let mut guard = self.inner.lock().unwrap_or_else(PoisonError::into_inner);
            let (map, clock, closed) = &mut *guard;
            if *closed {
                stmt.session_ending.store(true, Ordering::Release);
                return stmt;
            }
            *clock += 1;
            if let Some(e) = map.get_mut(sql.as_str()) {
                e.last_used = *clock;
                return e.stmt.clone();
            }
            evicted = if map.len() >= self.capacity {
                let oldest = map
                    .iter()
                    .min_by_key(|(_, e)| e.last_used)
                    .map(|(k, _)| k.clone());
                oldest.and_then(|k| map.remove(&k))
            } else {
                None
            };
            map.insert(
                sql.into_boxed_str(),
                Entry {
                    stmt: stmt.clone(),
                    last_used: *clock,
                },
            );
            result = stmt;
        }
        // Dropped outside the lock. In-flight queries keep their own Arc, so the server-side
        // statement is only closed once nobody uses it.
        drop(evicted);
        result
    }

    /// Remove `stmt` if it is still the cached entry for its SQL (stale statements).
    pub fn evict(&self, stmt: &Arc<CachedStmt>) {
        let removed = {
            let mut guard = self.inner.lock().unwrap_or_else(PoisonError::into_inner);
            let map = &mut guard.0;
            match map.get(stmt.sql()) {
                Some(e) if Arc::ptr_eq(&e.stmt, stmt) => map.remove(stmt.sql()),
                _ => None,
            }
        };
        drop(removed);
    }

    pub fn len(&self) -> usize {
        self.inner
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .0
            .len()
    }

    /// Drop every entry, and keep none from now on (the connection is closing: a cached
    /// statement would keep its client, and so the connection, alive). Their server-side
    /// statements are left to the session's end.
    pub fn close(&self) {
        let drained: Vec<Entry> = {
            let mut guard = self.inner.lock().unwrap_or_else(PoisonError::into_inner);
            guard.2 = true;
            guard.0.drain().map(|(_, e)| e).collect()
        };
        for entry in &drained {
            entry.stmt.session_ending.store(true, Ordering::Release);
        }
        drop(drained);
    }
}
