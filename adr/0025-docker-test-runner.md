# 0025. Docker test and benchmark runner

- Status: Accepted
- Date: 2026-10-10

## Context

Linux behaviour must be checked without a local Rust toolchain, and next to PostgreSQL.

## Decision

`task test-docker` builds fastpg on Linux and runs both test suites against the compose
database; `task bench-docker` runs the benchmark suite the same way.

- The image is `debian:trixie-slim` plus a pinned uv binary (0.12.24). The floating
  `uv:*-slim` tags ship uv 0.9, which cannot install Python 3.14.7.
- uv installs exactly the 3.14.7 from `.python-version`; the python:3.14 images ship 3.14.2,
  which `requires-python >=3.14.3` rejects.

## Consequences

Linux-only bugs are caught (see [0055](0055-treat-read-errors-as-eof.md)). The pinned uv version must be bumped by hand.
