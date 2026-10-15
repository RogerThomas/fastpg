# Architecture Decision Records

Each record describes one significant decision in fastpg's current design: its context, the decision and its consequences.

| Number | Title |
|---|---|
| [0001](0001-rust-core-on-xitca-postgres.md) | Rust core on xitca-postgres |
| [0002](0002-protocol-on-the-event-loop.md) | Protocol on the event loop |
| [0003](0003-typed-results-with-into.md) | Typed results with into= |
| [0004](0004-binary-format-and-statement-cache.md) | Binary format and statement cache |
| [0005](0005-connection-pool.md) | Connection pool |
| [0006](0006-query-parameters.md) | Query parameters |
| [0007](0007-bulk-writes.md) | Bulk writes |
| [0008](0008-tls.md) | TLS |
| [0009](0009-errors.md) | Errors |
| [0010](0010-api-shape.md) | API shape |
| [0011](0011-scope-and-platform.md) | Scope and platform |

## Open questions

- An intermittent 35-68 s connect stall, seen only on connects through Docker Desktop's host port forward on macOS (TCP retransmission times; the connect then succeeds). It is not TLS-related (see [notes/tls.md](../notes/tls.md), section 6) and does not reproduce container to container.
- No test covers a pool over TLS; `tests/test_tls.py` covers single connections only.
- `tests/test_loop_io.py` largely duplicates `tests/test_connection.py`; the two could be consolidated.
