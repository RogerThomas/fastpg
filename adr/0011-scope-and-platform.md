# 0011. Scope and platform

## Context

Supporting several databases, older interpreters or every platform would shape the design
around the lowest common denominator. The protocol design ([ADR 0002](0002-protocol-on-the-event-loop.md))
also depends on what the event loop offers.

## Decision

- PostgreSQL only, with no multi-backend structure.
- CPython 3.13 or later (`requires-python >=3.13`): 3.13, 3.14 and 3.15. No release needs a
  particular patch version. t-string queries (PEP 750) need 3.14: on 3.13 everything else
  works, `fastpg._template` stands in for `string.templatelib` so that no query is a
  `Template`, and `Identifier` is still exported (it has no use there). The only other
  version-specific code is the tuple hash cache CPython 3.14 added (`native/src/structs.rs`,
  `#[cfg(Py_3_14)]`). Code must run without 3.14's syntax and deferred annotations (names an
  annotation uses are imported at runtime): ruff and pyright check it as 3.13, the files with
  t-string literals as 3.14. The crate uses Rust edition 2024.
- macOS and Linux. Windows is unsupported because the socket is driven through
  `loop.add_reader` / `add_writer`, which its default proactor loop does not provide.
- The extension module declares `gil_used = true`: it has not been audited for
  free-threaded CPython, so importing fastpg on 3.14t or 3.15t re-enables the GIL. There are
  no 3.13t wheels: free threading was experimental in 3.13.
- MIT licence.

## Consequences

- Users on Python 3.12 or earlier, or on Windows, cannot use fastpg. On 3.13 they write
  queries with `$n` or `:name` parameters.
- Free-threaded builds run fastpg, but without parallelism until an audit allows the
  declaration to be dropped.
- PostgreSQL-specific features such as binary COPY, SQLSTATE classes and
  `sslnegotiation=direct` need no abstraction layer.
