# 0004. Build with maturin

- Status: Accepted
- Date: 2026-10-10

## Context

fastpg is a Rust extension module (PyO3), so the pure-Python `uv_build` backend from the
template cannot build it.

## Decision

- The build backend is `maturin` (`[tool.maturin]`), with the Rust crate in `native/`.
- `tool.uv.cache-keys` includes the Rust sources, so `uv sync` rebuilds when they change.
- The extension is built in release mode with `lto = "fat"` and `codegen-units = 1`.

## Consequences

Builds are slower than a default release build, in exchange for the best runtime
performance. A plain `uv sync` is enough to pick up Rust changes during development.
