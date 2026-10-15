# 0011. Scope and platform

## Context

Supporting several databases, older interpreters or every platform would shape the design
around the lowest common denominator. The protocol design ([ADR 0002](0002-protocol-on-the-event-loop.md))
also depends on what the event loop offers.

## Decision

- PostgreSQL only, with no multi-backend structure.
- CPython 3.14 or later (`requires-python >=3.14.3`), with no compatibility code for older
  versions; t-strings need 3.14. The crate uses Rust edition 2024.
- macOS and Linux. Windows is unsupported because the socket is driven through
  `loop.add_reader` / `add_writer`, which its default proactor loop does not provide.
- The extension module declares `gil_used = true`: it has not been audited for
  free-threaded CPython, so importing fastpg on 3.14t re-enables the GIL.
- MIT licence.

## Consequences

- Users on earlier Python versions or on Windows cannot use fastpg.
- Free-threaded builds run fastpg, but without parallelism until an audit allows the
  declaration to be dropped.
- PostgreSQL-specific features such as binary COPY, SQLSTATE classes and
  `sslnegotiation=direct` need no abstraction layer.
