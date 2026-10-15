# 0033. Integrate pydantic through its Python-level entry points

- Status: Accepted
- Date: 2026-10-10

## Context

pydantic-core has no Rust API and is not on crates.io. Vendoring it would give a second copy
whose type objects and schemas are not shared with the application's pydantic.

## Decision

Use pydantic-core's Python-level native entry points. Nothing reaches into pydantic-core
internals.

## Consequences

The integration works with whichever pydantic version is installed, at the cost of going
through Python-level calls.
