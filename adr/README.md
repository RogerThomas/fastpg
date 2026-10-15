# Architecture Decision Records

Each record captures one significant design decision for fastpg: its context, the choice made and its consequences.

Numbers 0001, 0002, 0042 and 0043 are unused.

| Number | Title | Status |
|---|---|---|
| [0003](0003-minimal-runtime-dependencies.md) | Minimal runtime dependencies | Accepted |
| [0004](0004-maturin-build-backend.md) | Build with maturin | Accepted |
| [0005](0005-no-pyo3-async-runtimes.md) | No pyo3-async-runtimes; Python objects only on the loop thread | Partly superseded by [0057](0057-loop-native-only-io-path.md) |
| [0006](0006-single-tokio-io-thread.md) | One tokio I/O thread by default | Superseded by [0057](0057-loop-native-only-io-path.md) and [0059](0059-tokio-without-runtime.md) |
| [0007](0007-stream-large-results.md) | Stream large results in chunks | Accepted |
| [0008](0008-raw-row-access-via-fromsql.md) | Raw row access through a private FromSql type | Accepted |
| [0009](0009-no-connection-pool.md) | No connection pool | Superseded by [0044](0044-pool-over-single-connection.md) and [0051](0051-pool-design.md) |
| [0010](0010-per-connection-statement-cache.md) | Per-connection LRU statement cache | Accepted |
| [0011](0011-connection-and-stateless-cursor.md) | Connection plus stateless Cursor API | Partly superseded by [0062](0062-queries-on-connection.md) |
| [0012](0012-strict-select-one.md) | Strict select_one | Accepted |
| [0013](0013-tuples-by-default.md) | Rows are tuples without into= | Accepted |
| [0014](0014-auto-strategy-default.md) | strategy="auto" by default | Accepted |
| [0015](0015-msgspec-column-matching.md) | Column matching follows msgspec decoding | Accepted |
| [0016](0016-direct-mode-type-compatibility.md) | Type compatibility in direct mode | Accepted |
| [0017](0017-unsupported-types.md) | Unsupported types fail with clear errors | Accepted |
| [0018](0018-strict-parameter-types.md) | Strict parameter type checks | Accepted |
| [0019](0019-uuid-fast-path.md) | UUID construction fast path | Accepted |
| [0020](0020-structs-built-via-init.md) | Structs are built through __init__ | Accepted |
| [0021](0021-postgresql-18-benchmark-server.md) | PostgreSQL 18.6 development and benchmark server | Accepted |
| [0022](0022-deterministic-seed.md) | Deterministic seed data | Accepted |
| [0023](0023-python-314-only.md) | Python 3.14 only, Rust edition 2024 | Accepted |
| [0024](0024-benchmark-drivers.md) | Benchmark drivers and settings | Accepted |
| [0025](0025-docker-test-runner.md) | Docker test and benchmark runner | Accepted |
| [0026](0026-detect-server-closed-connections.md) | Detect server-closed connections | Partly superseded by [0057](0057-loop-native-only-io-path.md) |
| [0027](0027-reprepare-stale-statements.md) | Re-prepare stale statements once | Accepted |
| [0028](0028-cancelled-connect-closes-session.md) | A cancelled connect does not leak a session | Accepted |
| [0029](0029-resourcewarning-for-unclosed-connections.md) | ResourceWarning for unclosed connections | Accepted |
| [0030](0030-kw-only-struct-fields.md) | Respect kw_only Struct fields | Accepted |
| [0031](0031-void-columns-decode-as-none.md) | void columns decode as None | Accepted |
| [0032](0032-accept-either-error-on-backend-kill.md) | Accept either error after a backend kill | Accepted |
| [0033](0033-pydantic-via-python-entry-points.md) | Integrate pydantic through its Python-level entry points | Accepted |
| [0034](0034-optional-pydantic-support.md) | Optional pydantic support | Accepted |
| [0035](0035-pydantic-json-and-direct-routes.md) | Two pydantic routes: json and direct | Accepted |
| [0036](0036-model-construct-semantics.md) | Directly built models follow model_construct semantics | Accepted |
| [0037](0037-json-strategy-for-structs.md) | json strategy for msgspec Structs | Accepted |
| [0038](0038-json-encoding-conventions.md) | JSON encoding conventions | Accepted |
| [0039](0039-rename-select-all-to-select-many.md) | Rename select_all to select_many | Accepted |
| [0040](0040-keyword-only-example-parameters.md) | Keyword-only parameters in examples | Accepted |
| [0041](0041-open-loop-benchmark.md) | Open-loop API traffic benchmark | Accepted |
| [0044](0044-pool-over-single-connection.md) | A pool rather than one pipelined connection | Accepted |
| [0045](0045-t-string-parameters.md) | Parameters from t-strings | Partly superseded by [0065](0065-named-parameters.md) |
| [0046](0046-name-and-postgresql-only-scope.md) | Keep the name fastpg; PostgreSQL only | Partly superseded by [0057](0057-loop-native-only-io-path.md) |
| [0047](0047-t-string-rules.md) | t-string rules | Accepted |
| [0048](0048-bulk-writes.md) | Bulk writes | Accepted |
| [0049](0049-read-write-gate.md) | Per-connection read/write gate | Accepted |
| [0050](0050-transaction-probe-for-bulk-writes.md) | Detect open transactions with a probe | Accepted |
| [0051](0051-pool-design.md) | Pool design | Accepted |
| [0052](0052-fail-pipelined-queries-after-fatal-error.md) | Fail queries pipelined behind a fatal error | Accepted |
| [0053](0053-read-write-gate-cost.md) | The read/write gate has no measurable cost | Accepted |
| [0054](0054-style-guide-exceptions.md) | Style guide applied repo-wide, with justified exceptions | Accepted |
| [0055](0055-treat-read-errors-as-eof.md) | Treat fatal socket errors as end-of-stream | Accepted |
| [0056](0056-loop-native-driver.md) | Move to a loop-native driver | Accepted |
| [0057](0057-loop-native-only-io-path.md) | Loop-native I/O is the only I/O path | Accepted |
| [0058](0058-delete-spin-busy-poll.md) | Delete the spin busy-poll | Accepted |
| [0059](0059-tokio-without-runtime.md) | tokio without a runtime | Accepted |
| [0060](0060-tls-with-rustls.md) | TLS with rustls | Partly superseded by [0066](0066-channel-binding-authentication.md) |
| [0061](0061-loop-path-hardening.md) | Harden the loop path | Accepted |
| [0062](0062-queries-on-connection.md) | Queries run on the Connection; Cursor is private | Accepted |
| [0063](0063-db-api-exception-names.md) | DB-API 2.0 exception names | Accepted |
| [0064](0064-pool-free-connection-first.md) | Pool routing: free connection first | Accepted |
| [0065](0065-named-parameters.md) | Named :name parameters via params= | Accepted |
| [0066](0066-channel-binding-authentication.md) | channel_binding via fastpg's own authentication | Accepted |
| [0067](0067-io-thread-busy-poll.md) | Busy-poll the I/O thread after each query | Superseded by [0058](0058-delete-spin-busy-poll.md) |

## Open questions

- An intermittent 35-68 s connect stall, seen only on connects through Docker Desktop's host port forward on macOS (TCP retransmission times; the connect then succeeds). The pre-TLS code shows it too, so it is not TLS-related (see [notes/tls.md](../notes/tls.md), section 6). Not reproduced container to container.
- No test covers a pool over TLS; `tests/test_tls.py` covers single connections only.
- `tests/test_loop_io.py` largely duplicates `tests/test_connection.py`, from when loop I/O was opt-in. The two could be consolidated.
