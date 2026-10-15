# 0066. channel_binding via fastpg's own authentication

- Status: Accepted
- Date: 2026-10-12

## Context

Hosted DSNs such as Neon's use `?sslmode=require&channel_binding=require`, which [0060](0060-tls-with-rustls.md)
rejected. xitca 0.4 picks the SASL mechanism itself, accepts password/md5 and `AuthenticationOk`
without SCRAM, and does not report what it used, so `require` cannot be enforced through it.
Without the end-point hash it sends gs2 flag `y`, which a server offering SCRAM-SHA-256-PLUS
rejects.

## Decision

For `require` and `disable`, fastpg authenticates itself (`native/src/auth.rs`): after TLS
it sends the startup message and answers with `postgres-protocol`'s encoders and SCRAM client,
up to `AuthenticationOk`. xitca then starts over a wrapper that drops its startup message and
replays the server's messages, including an `ErrorResponse`. `prefer` still authenticates in
xitca.

## Consequences

- `require` fails with `InterfaceError`, before sending any password, without TLS, without
  SCRAM-SHA-256-PLUS, on password/md5, or on `AuthenticationOk` before a verified exchange.
- `disable` uses SCRAM-SHA-256 with `n`, which also makes SCRAM over TLS work with
  Ed25519/Ed448 certificates.
- trust/md5/password cases are tested against a fake TLS server in `tests/test_tls.py`.
