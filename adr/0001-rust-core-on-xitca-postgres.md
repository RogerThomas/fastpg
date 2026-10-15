# 0001. Rust core on xitca-postgres

## Context

fastpg exists to turn PostgreSQL rows into Python objects faster than existing drivers.
Per-row work done in Python dominates the cost of large results, and the PostgreSQL wire
protocol is a large surface to implement and keep correct.

## Decision

- The core is a Rust extension module built with PyO3 and packaged with maturin (crate in
  `native/`, release builds with fat LTO and one codegen unit). The Python package
  (`src/fastpg`) holds the public API, the pool and type planning.
- xitca-postgres does the protocol work: startup, prepare, bind, execute, pipelining and
  COPY. fastpg has no wire protocol of its own; it only drives authentication itself when
  channel binding must be enforced ([ADR 0008](0008-tls.md)).
- xitca-postgres 0.4 exposes cells only through typed `FromSql`. fastpg implements `FromSql`
  for a private `RawCell` type that accepts every PostgreSQL type and returns the untouched
  binary value, which is copied into a per-result arena. xitca's zero-copy `get_zc`
  accessor is not used, because it reports empty non-NULL values such as `''` as NULL.

## Consequences

- Rows reach the decoder with one `memcpy` per cell, and empty values stay distinct from
  NULL.
- fastpg depends on xitca's public `FromSql` contract rather than its internals, and xitca is
  not vendored. Gaps in xitca 0.4, such as per-statement results behind one `Sync`, are
  worked around in fastpg ([ADR 0007](0007-bulk-writes.md)) or raised upstream.
- Building from source needs a Rust toolchain, and release builds are slower than defaults.
