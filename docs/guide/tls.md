# TLS

Set TLS options in the DSN, with the same keys as libpq and `psql`.

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
| `sslrootcert` | a CA certificate file, or `system` |
| `sslcert`, `sslkey` | a client certificate and its key |
| `sslnegotiation` | `postgres` (default) or `direct` |
| `sslsni` | `1` (default) or `0` |
| `channel_binding` | `disable`, `prefer` (default) or `require` |

* `prefer` and `require` encrypt the connection, but only check the server's certificate if
  you pass `sslrootcert`.
* Use `verify-full` to check the certificate and the host name.
* Without `sslrootcert`, fastpg trusts your system's certificates.
  For Amazon RDS, pass the RDS CA bundle as `sslrootcert`.

Some libpq options aren't supported, such as `sslmode=allow` and password-protected keys.
An unsupported option raises `InterfaceError` before fastpg connects.

## Try it locally

The repository has a TLS-only test server on port 55433:

```sh
task db-tls-up
```

It creates its certificates in `docker/tls/certs/` the first time it starts.
