# 0021. PostgreSQL 18.6 development and benchmark server

- Status: Accepted
- Date: 2026-10-10

## Context

Benchmarks compare drivers, so the server must never be the bottleneck. PostgreSQL 19 was
only available as a beta.

## Decision

Use `postgres:18.6-trixie` (Debian/glibc rather than Alpine/musl), tuned for throughput:

- `max_connections=1000`, `shared_buffers=2GB` (the ~21 MB database is prewarmed with
  `pg_prewarm`), `effective_cache_size=6GB`, `work_mem=32MB`;
- `jit=off`, removing multi-ms JIT jitter;
- durability off: `fsync`, `synchronous_commit`, `full_page_writes` off, `wal_level=minimal`;
- `shm_size: 2gb`, `nofile` 65536, no CPU or memory caps, data on tmpfs.

It listens on 127.0.0.1:55432 with user, password and database `fastpg`.

## Consequences

Every start is a fresh seed. Port 55432 avoids a local 5432. The healthcheck uses TCP, so it
only passes once the seed is loaded. The settings are unsafe for real data.
