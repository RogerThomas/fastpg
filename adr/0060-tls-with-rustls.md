# 0060. TLS with rustls

- Status: Partly superseded by [0066](0066-channel-binding-authentication.md)
- Date: 2026-10-10

## Context

Hosted PostgreSQL requires TLS. Details are in [notes/tls.md](../notes/tls.md).

## Decision

rustls (ring provider) runs between the socket and xitca, under `EofWatch`, so dead-connection
detection works through TLS. Supported: libpq's `sslmode` (`prefer` by default, as in libpq),
`sslrootcert`, `sslcert`/`sslkey`, `sslsni` and `sslnegotiation=direct`. SCRAM over TLS is
always channel-bound. `allow`, `sslpassword` and `channel_binding` other than `prefer` are
rejected with a clear error.

## Consequences

[0066](0066-channel-binding-authentication.md) adds `channel_binding=require` and `disable`.
