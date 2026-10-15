# 0046. Keep the name fastpg; PostgreSQL only

- Status: Partly superseded by [0057](0057-loop-native-only-io-path.md)
- Date: 2026-10-10

## Context

A rename and a multi-backend design (including a backend-picking `connect(url)`) were
considered.

## Decision

The name stays `fastpg` and the library is PostgreSQL only, with no multi-backend structure.

## Consequences

Should this come back, `squickl` was free on PyPI and crates.io as of 2026-10-10, and the
design (type planning, Struct/pydantic builders, t-string placeholders) would not block adding
backends. The "mailbox I/O thread" part of that design is superseded by [0057](0057-loop-native-only-io-path.md).
