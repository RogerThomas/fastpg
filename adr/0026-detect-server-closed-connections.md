# 0026. Detect server-closed connections

- Status: Partly superseded by [0057](0057-loop-native-only-io-path.md)
- Date: 2026-10-10

## Context

xitca-postgres 0.4's driver does not notice when the server closes the socket (restart,
`pg_terminate_backend`, idle timeout). It waits for the client to write again, and a query
sent afterwards could wait forever.

## Decision

fastpg opens the TCP or unix socket itself and wraps it in an EOF detector
(`native/src/net.rs`); on EOF the driver is dropped. Every pending query then fails promptly
and `conn.closed` becomes `True`.

Since [0057](0057-loop-native-only-io-path.md), Python opens the socket, and there is no fallback to xitca's own connect: a
host that does not parse fails with an `InterfaceError` naming it.

## Consequences

Tests cover a query issued after the kill and one in flight during it. xitca keeps
`Host`/`SslMode` private, so hosts are read from their `Debug` form; this is worth reporting
upstream.
