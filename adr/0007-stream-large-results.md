# 0007. Stream large results in chunks

- Status: Accepted
- Date: 2026-10-10

## Context

Buffering a whole large result before decoding serialises network transfer and decoding.
With 10,000 rows this made fastpg slower than asyncpg.

## Decision

`select_many` hands rows to the decoder in chunks of 512 (1,024 before the work in
[notes/perf-decode.md](../notes/perf-decode.md)), so chunk N is decoded while chunk N+1 is
still arriving.

## Consequences

The 10,000-row case went from slower than asyncpg to faster than it. Memory held for a large
result is bounded per chunk on the receive side.
