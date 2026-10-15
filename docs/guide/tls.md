# TLS

TLS is configured with libpq's DSN keys, and they behave as in libpq.

```python
dsn = (
    "postgresql://fastpg:fastpg@localhost:55433/fastpg"
    "?sslmode=verify-full&sslrootcert=docker/tls/certs/ca.crt"
)
query = "SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
async with await fastpg.connect(dsn) as tls_conn:
    print(await tls_conn.select_one(query))  # (True, 'TLSv1.3')
```

| key | values |
|---|---|
| `sslmode` | `disable`, `prefer` (default), `require`, `verify-ca`, `verify-full` |
| `sslrootcert` | a PEM file or bundle, or `system` (needs `verify-full`) |
| `sslnegotiation` | `postgres` (default), or `direct` (PostgreSQL 17+, needs `require` or stronger) |
| `sslcert`, `sslkey` | a PEM client certificate and its unencrypted key, both or neither |
| `sslsni` | `1` (default), `0` |
| `channel_binding` | `disable`, `prefer` (default), `require` |

* `prefer` and `require` encrypt without verifying (unless an `sslrootcert` file exists);
  `verify-ca` checks the chain, and `verify-full` also the host name, against DNS or IP
  subject alternative names. `prefer` falls back to plaintext only when the server declines
  TLS.
* Without `sslrootcert`, verification uses the system's roots (`SSL_CERT_FILE` /
  `SSL_CERT_DIR` when set), or Mozilla's compiled-in roots if the system has none. For
  Amazon RDS, pass the RDS CA bundle as `sslrootcert`.
* With `prefer`, SCRAM logins over TLS are channel-bound (SCRAM-SHA-256-PLUS) whenever the
  server offers it, so they fail against a server whose certificate is signed with Ed25519
  or Ed448; `disable` never binds.
* `channel_binding=require` only accepts a channel-bound SCRAM login over TLS. Without TLS,
  or when the server asks for a password or md5, or skips authentication (`trust`), the
  connect fails with `InterfaceError` before any password is sent.
* Unix-socket connections never use TLS.

## Not supported

`sslmode=allow`, encrypted client keys (`sslpassword`), certificate revocation lists,
`ssl_min_protocol_version` / `ssl_max_protocol_version`, `sslcertmode`, `sslkeylogfile`,
`gssencmode`, `hostaddr`, TLS session resumption, and anything from `~/.postgresql/` or
`PG*` environment variables. An unsupported setting fails the connect with `InterfaceError`
before any connection is opened.

## The test server

```sh
task db-up db-tls-up    # a TLS-only PostgreSQL on localhost:55433, plus the plain one
task test-tls           # tests/test_tls.py
```

It creates a throwaway CA and certificates in `docker/tls/certs/` on first start.
