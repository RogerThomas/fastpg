# TLS (`sslmode`)

> Written when there were two connection paths, the threaded (tokio) one and inline mode.
> Inline mode is now the only one ([ADR 0002](../adr/0002-protocol-on-the-event-loop.md) and [ADR 0008](../adr/0008-tls.md)), so "both paths" below means "the
> loop-native path", and the threaded rows in section 7 are history.

Branch `tls`, on top of the inline-mode prototype. Code: `native/src/tls.rs` (DSN keys,
negotiation, handshake, the TLS `AsyncIo`, channel binding), `native/src/net.rs`
(`config`, `handshake` now negotiate TLS first), `native/src/connection.rs` (`attach` takes
the host name), `src/fastpg/_connection.py` (inline mode passes the host; reader registered
before `attach`). Tests: `tests/test_tls.py`, `native/src/tls.rs` unit tests. Test server:
`docker/tls/` and the compose service `postgres-tls` (profile `tls`).

**Verdict:** one implementation serves both paths. A rustls `ClientConnection` (sans-I/O)
runs inside an `AsyncIo` adapter between the socket and xitca, below the existing
`EofWatch`; xitca still does all of the PostgreSQL protocol through `Postgres::connect_io`.
The whole suite (425 tests, 52 of them TLS) passes on macOS and on Linux; TLS costs about
1–2 µs of CPU per point query (section 7).

## 1. What xitca-postgres 0.4 offers, and why it isn't used

References are to `~/.cargo/registry/src/*/xitca-postgres-0.4.0/src`.

* **The `tls` feature** (`Cargo.toml`: `tls = ["dep:xitca-tls",
  "xitca-tls/rustls-ring-crypto", "dep:sha2", "dep:webpki-roots"]`) only acts inside
  `Postgres::connect()`: `driver/connect.rs:38-62` calls `should_connect_tls` and then
  `tls::connect_tls` on the TCP/unix stream xitca opened itself.
* **`connect_io` skips all of it.** `driver.rs:74-80` goes straight to `prepare_driver`
  (startup and authentication) with `ConnectInfo::default()`. `should_connect_tls`
  (`driver.rs:99-142`, the `SSLRequest` exchange) and `connect_tls` (`driver/tls.rs:17`,
  `pub(super)`) are private, so they can't be called on our socket. The TLS driver variants
  (`driver.rs:203-214`, `Driver::Tls(GenericDriver<TlsStream<ClientConnection, TcpStream>>)`,
  `UnixTls`) are tied to xitca's own stream types, so inline mode's `LoopSocket` couldn't use
  them either.
* **xitca's TLS doesn't verify anything.** `driver/tls.rs:49-62` (`dangerous_config`) loads
  the webpki roots and then installs `SkipServerVerification` (`driver/tls.rs:64-118`), which
  accepts any certificate. So the feature amounts to libpq's `require`: there is no
  `verify-ca`/`verify-full`, no `sslrootcert`.
* **Its DSN parser rejects the libpq TLS keys.** `config.rs:328-336` accepts only
  `sslmode=disable|prefer|require` (anything else is `Error::todo()`), `config.rs:337-344`
  `sslnegotiation=postgres|direct`, and unknown keys such as `sslrootcert` fail at
  `config.rs:369-371`. The feature also flips the default `SslMode` from `Disable` to
  `Prefer` (`config.rs:12-23`).
* **Its channel binding hash is always SHA-256** (`driver/tls.rs:37-44`). RFC 5929 (and
  PostgreSQL) hash with the certificate signature's own hash, so SCRAM fails against a server
  whose certificate is signed with SHA-384 or SHA-512 (as the test server's is).
* **What is reused:** `Postgres::connect_io` + `AsyncIo` (as before), xitca's documented hook
  for exactly this case, `Config::tls_server_end_point` (`config.rs:234-305`: "often coupled
  with `connect_io` for manual tls session connecting and channel binding"), and
  `postgres-protocol`'s `frontend::ssl_request`, the encoder xitca itself uses. No wire
  protocol is hand-written; the only bytes read outside xitca are the one-byte answer to
  `SSLRequest`.

### A consequence found while testing: channel binding is mandatory

Over TLS, PostgreSQL offers `SCRAM-SHA-256-PLUS`. When `tls_server_end_point` is not set,
xitca (`session.rs:192-206`) answers with `ChannelBinding::unrequested()`, which
`postgres-protocol` encodes as the gs2 flag `y` ("I support channel binding but think you
don't", `authentication/sasl.rs:98`). The server treats that as a downgrade attack and fails
the login: *SCRAM channel binding negotiation error (28000)*. So with `connect_io` over TLS,
SCRAM authentication works only when the end-point hash is supplied. fastpg always supplies
it (section 4), which is why `channel_binding=disable` can't be offered.

## 2. Design

```
 xitca driver ──read/write plaintext──▶ EofWatch ──▶ TlsStream ──TLS records──▶ socket
                                         │ (EOF, bytes written)  rustls ClientConnection
                                         ▼                          │
                                  IoWatch.eof → serve() drops the driver → dead → pending ops fail
 socket = LoopSocket (fd connected and owned by Python)
```

`net::handshake(io, cfg, tls, host, eof)` is shared by both paths:

1. **Negotiation** (`tls::start`). `disable`: nothing. Otherwise the `ClientConfig` is built
   first (so a missing file fails before any I/O), then `SSLRequest` is sent and exactly one
   byte read: `S` → TLS; `N` → plaintext for `prefer`, *"server does not support SSL, but
   SSL was required"* otherwise. Reading only that byte keeps anything an attacker queued
   behind it inside the TLS handshake, where it fails (libpq's CVE-2021-23222). With
   `sslnegotiation=direct` the handshake starts immediately, and the server must select the
   ALPN protocol `postgresql`.
2. **Handshake** (`tls::handshake`): rustls' `write_tls`/`read_tls`/`process_new_packets`
   loop over the socket's `AsyncIo`, waiting with `io.ready(..)` on `WouldBlock`. rustls
   errors become `InterfaceError`s with a hint (unknown issuer → set `sslrootcert`; wrong
   name → use the certificate's name or `verify-ca`).
3. **Channel binding:** the server certificate's `tls-server-end-point` hash goes into
   `Config::tls_server_end_point`.
4. **Startup:** `EofWatch { TlsStream { socket, rustls } }` → `connect_io`, unchanged.

**`TlsStream` (an `AsyncIo`)**

* `read`: return buffered plaintext; when there is none, `read_tls` once from the socket and
  decrypt, until the socket says `WouldBlock`. rustls' plaintext buffer is drained before
  more records are read, so it never fills up. Records rustls wants to send in response (a
  key update, an alert) are flushed on the way.
* readiness: after a read that did not end in `WouldBlock`, `ready(READABLE)` returns at once
  (plaintext may be left, or the session ended); otherwise the socket's readiness is used.
  `ready()` delegates to the socket's own `ready()`, because tokio streams can't
  `poll_ready` both directions at once (`xitca-io` `net.rs`, `unimplemented!`).
* `write`: first flush earlier records (`WouldBlock` if the socket is still full), then
  encrypt up to rustls' 64 KiB limit and try to send it. So at most one batch of ciphertext is
  buffered, and `WouldBlock` still means "the socket is full". Leftover records go out with
  `flush()`, which xitca calls after every write burst (its `WantFlush` state).
* `poll_shutdown` sends `close_notify`, flushes, then shuts the socket down.

**Dead-connection detection through TLS ([ADR 0002](../adr/0002-protocol-on-the-event-loop.md)).** `EofWatch` now sees
plaintext, so every way a TLS connection ends reaches it:

| what happens | rustls / `TlsStream::read` | `EofWatch` |
|---|---|---|
| server sends `close_notify` (PostgreSQL does on exit, e.g. `pg_terminate_backend`) | `Ok(0)` | 0-byte read → EOF |
| TCP FIN without `close_notify` (crash, proxy, network) | `Err(UnexpectedEof)` | fatal error → EOF |
| TCP reset (Linux `ECONNRESET`, [ADR 0002](../adr/0002-protocol-on-the-event-loop.md)) | socket error passed through | fatal error → EOF |
| TLS alert / corrupt record | `Err(InvalidData)` | fatal error → EOF |
| write after the peer is gone (`EPIPE`) | socket error | fatal error → EOF |

Tests cover the first row (`test_server_terminating_connection_fails_promptly`, three
in-flight `pg_sleep(30)`s fail within 5 s and `conn.closed` turns true) and the second
(`test_connection_cut_without_close_notify_fails_promptly`, through a TCP proxy that aborts
both sides), on both paths, on macOS and Linux.

**COPY backpressure** (`bulk::wait_for_socket`) counts the bytes `EofWatch` passes down,
i.e. plaintext handed to TLS. Because `TlsStream` stops accepting plaintext while a batch
of ciphertext is unsent, that count still trails the socket by at most 64 KiB.

**DSN handling.** `tls::split_dsn` takes `sslmode`, `sslrootcert`, `sslnegotiation`,
`sslsni`, `sslcert`, `sslkey`, `sslpassword`, `channel_binding` out of the DSN and hands
the rest to xitca untouched. It follows xitca's own grammar for both forms: URL (credentials
up to the first `@`, parameters after the next `?`, percent-decoded) and key/value (quotes,
backslash escapes). Later keys win, as in xitca. `dsn_targets()` parses through the same
function, so inline mode reports bad TLS settings before it opens a socket.

**Inline mode.** Python passes the DSN host the socket reached (`attach(host=...)`), for SNI
and name verification. `loop.add_reader` is now called *before* `attach()`: a TLS setting
error makes the first pump fail the connect and detach (unregister and close) the socket
inside `attach()`, and registering a closed fd afterwards raised `EBADF` (found by
`test_invalid_settings_are_rejected[inline-...does not exist]`).

**Unix sockets never use TLS**, whatever `sslmode` says: libpq does the same (the kernel
already authenticates the peer).

## 3. Supported settings

| key | values | notes |
|---|---|---|
| `sslmode` | `disable`, `prefer` (**default**, as libpq), `require`, `verify-ca`, `verify-full` | `allow` is rejected (it would need a second connection attempt) |
| `sslrootcert` | a PEM file (bundles allowed), or `system` | `system` implies `verify-full` and rejects weaker modes (libpq 16) |
| `sslnegotiation` | `postgres` (default), `direct` (PostgreSQL 17+) | `direct` needs `require` or stronger (libpq 17) |
| `sslcert`, `sslkey` | PEM client certificate (chain) and unencrypted key | both or neither; PKCS#8, PKCS#1 and SEC1 keys |
| `sslsni` | `1` (default), `0` | SNI is only ever sent for DNS names |
| `channel_binding` | `disable`, `prefer` (default), `require` | `disable`/`require` were rejected at first; now supported ([ADR 0008](../adr/0008-tls.md), `native/src/auth.rs`) |
| `sslpassword` | — | rejected: encrypted keys aren't supported |

How the server certificate is checked:

| `sslmode` | chain | host name |
|---|---|---|
| `prefer`, `require` | only if an `sslrootcert` **file exists** (libpq's rule); a missing file is ignored | no |
| `verify-ca` | yes | no |
| `verify-full` | yes | yes (DNS or IP SAN; no CN fallback) |

Whatever the mode, the handshake signature is verified, so the server proves it holds the
key of the certificate it sent (which channel binding depends on). TLS 1.2 and 1.3, rustls'
safe defaults (ECDHE, AES-GCM/ChaCha20); PostgreSQL's own minimum is TLS 1.2.

## 4. Certificates, roots and channel binding

* **Roots for `verify-*`:** the `sslrootcert` file when given. Without one (or with
  `sslrootcert=system`), the operating system's trust store through `rustls-native-certs`:
  `SSL_CERT_FILE`/`SSL_CERT_DIR` if set, otherwise the platform store (macOS keychain
  trust settings; on Linux the distribution bundle, e.g. `/etc/ssl/certs/ca-certificates.crt`
  or Amazon Linux's `/etc/pki/tls/certs/ca-bundle.crt`). If that yields no certificates at
  all (scratch or distroless images), Mozilla's roots compiled in via `webpki-roots` are
  used. The store is loaded once per process (`OnceLock`); a `sslrootcert` file is read on
  every connect, so rotated files are picked up.
* **Why system roots first:** they follow the host's policy (corporate CAs, removals), and
  `SSL_CERT_FILE` lets a deployment point at a bundle without touching DSNs. The Mozilla
  fallback keeps `verify-full` usable in minimal images.
* **Deliberate difference from libpq:** libpq's default `sslrootcert` is
  `~/.postgresql/root.crt` (and `verify-*` fails if it is missing); fastpg reads no files
  from the home directory (also not `~/.postgresql/postgresql.crt`/`.key`), and uses the
  system roots instead. libpq has the same behaviour only with `sslrootcert=system`.
* **Managed databases:** Amazon RDS/Aurora certificates are issued by Amazon's RDS CAs,
  which are not in system or Mozilla stores: use `sslmode=verify-full&sslrootcert=<the RDS
  global bundle>` (or `require` for encryption without verification). Providers with
  publicly trusted certificates (e.g. Let's Encrypt) work with plain `sslmode=verify-full`.
* **Channel binding** (`tls-server-end-point`, RFC 5929): the server's leaf certificate,
  hashed with the hash of its signature algorithm, MD5 and SHA-1 upgraded to SHA-256, which
  is what PostgreSQL computes (`be_tls_get_certificate_hash`). The signature algorithm is read
  from the certificate's DER (`tls::server_end_point`): PKCS#1 RSA, ECDSA, and RSASSA-PSS
  (hash taken from its parameters, as PostgreSQL does through OpenSSL's
  `X509_get_signature_info`). Ed25519/Ed448 have no such hash;
  PostgreSQL can't bind to them either. Since xitca then sends `y` (section 1), **SCRAM over
  TLS fails with Ed25519/Ed448 server certificates**; other authentication methods are
  unaffected. The test server's certificate is signed ECDSA P-384 with SHA-384, so every
  SCRAM login over TLS in the tests checks the non-default hash.
* `channel_binding=require` can't be honoured through xitca: it doesn't say whether
  SCRAM-PLUS was used (the server may pick md5, password or trust). It was rejected rather
  than ignored; fastpg now runs the authentication itself for `require` and `disable`
  (`native/src/auth.rs`, [ADR 0008](../adr/0008-tls.md)).

## 5. Tests and the TLS test server

`tests/test_tls.py` (when written, each test ran on both paths; now there is one):

* against the TLS-only server (skipped unless `FASTPG_TLS_DSN` and `FASTPG_TLS_CA` are set):
  `verify-full` (+ SCRAM-PLUS), wrong host name (the server's IP address, which the
  certificate lacks) fails with *certificate not valid for name ... use sslmode=verify-ca*,
  `verify-ca` with that address succeeds, `verify-full` without `sslrootcert` and with
  `sslrootcert=system` fails with *UnknownIssuer ... set sslrootcert*, `require` and `prefer`
  encrypt, `prefer` is the default, `require` + an existing wrong root file fails while a
  missing one is ignored, `disable` is refused by the server (*no encryption*, 28000),
  `sslnegotiation=direct`, client-certificate login (role `fastpg_cert`, `cert` auth),
  queries/20k-row results/Structs/pipelining/`execute_many`/a 30k-row `copy_in` (~6 MB, far
  past the 64 KiB TLS buffer), `pg_terminate_backend` with in-flight queries, and a cut
  connection without `close_notify`;
* against the plain compose server: `prefer` falls back to plaintext, `disable`, `require`
  fails (*server does not support SSL*), and invalid settings are rejected (`allow`, bad
  values, weak `direct`, `system` + `require`, `channel_binding`, lone `sslcert`, missing
  root file).

The Rust unit tests (`cargo test`) cover DSN splitting (both forms, escapes, xitca accepts
the rest), libpq's option rules, and the end-point hash for each signature algorithm.

**The server** (`docker/tls/entrypoint.sh`, `docker/tls/pg_hba.conf`,
`docker/tls/initdb/01-roles.sql`): `postgres:18.6-trixie` with `ssl=on`; the entry point
creates a throwaway CA (ECDSA P-384, SHA-384), a server certificate for `localhost` and
`postgres-tls`, and a client certificate for `fastpg_cert` in `docker/tls/certs/` on first
start (gitignored, never committed), copies the server key where PostgreSQL accepts it
(owned by postgres, mode 0600), and starts the server. `pg_hba.conf` only has `hostssl`
lines, so plaintext TCP logins are rejected.

With compose (profile `tls`, port 55433):

```
docker compose --profile tls up -d --wait postgres-tls        # or: task db-tls-up
FASTPG_DSN=postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg \
FASTPG_TLS_DSN=postgresql://fastpg:fastpg@localhost:55433/fastpg \
FASTPG_TLS_CA=docker/tls/certs/ca.crt uv run pytest tests/test_tls.py   # or: task test-tls
docker compose --profile tls rm -sf postgres-tls              # or: task db-tls-down
```

By hand, without compose (what this work used, so the compose project was never touched):

```
docker run -d --name fastpg-tls-test -p 127.0.0.1:55433:5432 \
  -e POSTGRES_USER=fastpg -e POSTGRES_PASSWORD=fastpg -e POSTGRES_DB=fastpg \
  -v "$PWD/docker/tls:/tls:ro" -v "$PWD/docker/tls/initdb:/docker-entrypoint-initdb.d:ro" \
  -v "$PWD/docker/tls/certs:/certs" --entrypoint /tls/entrypoint.sh postgres:18.6-trixie
# ... run the tests as above ...
docker rm -f fastpg-tls-test
```

Delete `docker/tls/certs/` to get fresh certificates (they are valid for 30 days).

The Linux run used the `docker/Dockerfile.test` image on a private network with the TLS
container (`docker network create fastpg-tls-net`, `docker network connect --alias
postgres-tls fastpg-tls-net fastpg-tls-test`, then the image with
`FASTPG_TLS_DSN=postgresql://fastpg:fastpg@postgres-tls:5432/fastpg`,
`FASTPG_TLS_CA=/certs/ca.crt` and the certs directory mounted at `/certs`).

## 6. Results

* macOS: `uv run pytest` with the TLS environment: 425 passed (373 before + 52); the TLS
  file 5/5 runs green. Without the TLS environment: 22 passed, 30 skipped.
* Linux (Docker, `Dockerfile.test`, TLS server on a container network): `cargo test` 16
  passed; `pytest` 425 passed; the TLS file 3/3 more runs green.
* The compose service, started under a separate project name with freshly generated
  certificates (`docker compose -p fastpg-tls-check --profile tls up -d --wait
  postgres-tls`), then `task test-tls`: 52 passed.
* `cargo clippy -- -D warnings`, `cargo fmt --check`, `ruff check .`, `ruff format --check
  src tests bench examples`, `pyright .` (strict): clean.
* Unrelated, seen on Linux only: in some full-suite runs one connect to the compose server
  through Docker Desktop's host port forward stalls for ~35 or ~68 s (TCP retransmission
  times) and then succeeds. The pre-TLS base commit's image does the same (1 of 3 runs),
  so it is the Docker networking, not TLS; 400 back-to-back connects in a loop never
  stalled.

## 7. Cost (sanity check, not a benchmark)

Linux, container to container, one server accepting both (`sslmode=disable` vs `require`),
3,000 sequential `SELECT $1::int, 'abcdefghij'::text`, `FASTPG_SPIN_US=0` (the threaded
path's spin, since deleted), three rounds (scratch script, not committed):

| path | plaintext p50 / CPU per query | TLS p50 / CPU per query | 20k rows × 100 B, plain → TLS |
|---|---:|---:|---:|
| threaded (I/O thread, deleted) | 92–93 µs / 45–46 µs | 95–96 µs / 46 µs | 4.3–4.9 → 4.9–5.6 ms |
| inline (now the only path) | 56–58 µs / 22–24 µs | 60–62 µs / 24–25 µs | 3.9–4.2 → 4.4–4.5 ms |

TLS adds about 3–4 µs of latency and 1–2 µs of CPU per point query (AES-GCM plus one extra
copy each way), and ~0.5 ms per 2 MB of results: encryption only, no extra round trips per
query. Connecting costs one extra round trip for `SSLRequest` (none with `direct`) plus the
TLS 1.3 handshake (one more round trip and an ECDHE key exchange). On macOS (Docker
Desktop) the difference was inside the noise.

## 8. Limitations and possible follow-ups

* `sslmode=allow` (plaintext first, then TLS) is not supported, and `prefer` doesn't retry
  in plaintext when a TLS handshake *fails* (it does fall back when the server answers `N`).
  Both would need a second connection attempt.
* `channel_binding=disable|require` were rejected (section 4; now supported, [ADR 0008](../adr/0008-tls.md)).
  SCRAM over TLS with the default `prefer` fails with Ed25519/Ed448 server certificates
  (xitca sends gs2 `y`; `disable` works there). Upstream fix: xitca should send
  `n` ("client doesn't support binding") when it has no end-point data, and could report
  the mechanism it used, which would make `require` possible.
* Encrypted client keys (`sslpassword`), `sslcrl`/`sslcrldir`, `ssl_min_protocol_version`
  / `ssl_max_protocol_version`, `sslcertmode`, `sslkeylogfile`, `gssencmode` (GSSAPI
  encryption) and `hostaddr` are not supported (the last ones are rejected by xitca's parser
  as before, with its unhelpful error). No `PG*` environment variables are read
  (`PGSSLMODE`, `PGSSLROOTCERT`, ...), consistent with the rest of fastpg.
* Nothing is read from `~/.postgresql/` (section 4).
* No TLS session resumption (PostgreSQL disables tickets anyway: `SSL_OP_NO_TICKET`).
* The system trust store is loaded on the first `verify-*` connect without `sslrootcert`,
  on the loop thread that connects, once per process: measured
  ~7 ms on Debian (ca-certificates) and ~130 ms on macOS (keychain).
* Unix sockets never use TLS (libpq semantics).
* Server-side cancel (not implemented in fastpg) would need its own TLS connection when it
  comes.
