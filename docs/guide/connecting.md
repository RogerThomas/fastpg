# Connecting

Everything about how to connect goes in the connection string (the DSN) you pass to
`fastpg.connect()` or `fastpg.create_pool()`.
Their keyword arguments, such as `statement_cache_size` or the [pool](pool.md) sizes, never
change where or how fastpg connects.

## The DSN

A DSN is a URL or a list of `key=value` pairs, as with psql.
These two are the same:

```python
dsn = "postgresql://app:secret@db.example.com:5432/shop?connect_timeout=10"
dsn = "host=db.example.com port=5432 user=app password=secret dbname=shop connect_timeout=10"
```

In a URL, `postgres://` works too.
Write any other setting as `?key=value`, joined with `&`.
If the user name or password holds a character such as `@`, `:` or `/`, percent-encode it
(`@` is `%40`).

In the `key=value` form, put a value with spaces in single quotes: `password='a b'`.

An empty DSN, `fastpg.connect("")`, takes every setting from the environment or the defaults.

## Settings

| key | default | what it does |
|---|---|---|
| `host` | the local server's Unix socket | the server's name or IP address, or a directory that holds its Unix socket (a path starting with `/`) |
| `port` | `5432` | the server's port |
| `user` | your login name | the user to log in as |
| `password` | none | the user's password |
| `dbname` | the user name | the database to use |
| `passfile` | `~/.pgpass` | the [password file](#the-password-file) |
| `application_name` | none | a name for the connection, shown in `pg_stat_activity` |
| `options` | none | server settings for the session, e.g. `-c statement_timeout=5s` |
| `connect_timeout` | none: wait forever | seconds to wait for each server to connect and log in |
| `target_session_attrs` | `any` | `read-write` or `read-only` to skip servers that aren't (see [Several hosts](#several-hosts)) |
| `keepalives` | `1` | `0` turns TCP keepalives off |
| `keepalives_idle` | the OS default | seconds idle before the first keepalive |
| `keepalives_interval` | the OS default | seconds between keepalives |
| `keepalives_count` | the OS default | keepalives missed before the connection counts as lost |
| `tcp_user_timeout` | the OS default | milliseconds sent data may go unacknowledged (Linux only) |
| `sslmode` | `prefer` | `disable`, `prefer`, `require`, `verify-ca` or `verify-full` (see [TLS](#tls)) |
| `sslrootcert` | your system's certificates | a CA certificate file, or `system` |
| `sslcert`, `sslkey` | none | a client certificate and its key |
| `sslnegotiation` | `postgres` | `direct` starts TLS at once (PostgreSQL 17 and newer) |
| `sslsni` | `1` | `0` stops sending the host name during the TLS handshake |
| `channel_binding` | `prefer` | `disable`, `prefer` or `require` |

Any other key raises `InterfaceError` before fastpg connects, naming the key.
`sslpassword` isn't supported either: use a client key that isn't encrypted.

## Environment variables

A setting the DSN leaves out comes from its `PG*` environment variable, if that is set:

| key | variable |
|---|---|
| `host` | `PGHOST` |
| `port` | `PGPORT` |
| `user` | `PGUSER` |
| `password` | `PGPASSWORD` |
| `dbname` | `PGDATABASE` |
| `passfile` | `PGPASSFILE` |
| `application_name` | `PGAPPNAME` |
| `options` | `PGOPTIONS` |
| `connect_timeout` | `PGCONNECT_TIMEOUT` |
| `target_session_attrs` | `PGTARGETSESSIONATTRS` |
| `sslmode` | `PGSSLMODE` |
| `sslrootcert` | `PGSSLROOTCERT` |
| `sslcert`, `sslkey` | `PGSSLCERT`, `PGSSLKEY` |
| `sslnegotiation` | `PGSSLNEGOTIATION` |
| `sslsni` | `PGSSLSNI` |
| `channel_binding` | `PGCHANNELBINDING` |

The keepalive settings and `tcp_user_timeout` have no variable.

## Which value wins

For each setting, fastpg uses the first of:

1. the DSN;
2. its `PG*` environment variable;
3. for the password only, the password file;
4. the default.

## The password file

If neither the DSN nor `PGPASSWORD` gives a password, fastpg looks in `~/.pgpass`, as psql
does.
Each line is `host:port:database:user:password`, and `*` matches anything:

```text
db.example.com:5432:shop:app:secret
*:*:*:app:secret
```

The first line that matches is used.
For a Unix socket, write `localhost` as the host.
Only you may be able to read the file (`chmod 600 ~/.pgpass`): otherwise fastpg ignores it
and warns.

## Defaults

With no host, fastpg connects to the local server's Unix socket.
It tries `/var/run/postgresql`, `/run/postgresql` and `/tmp`, in that order, and uses the
first that holds one.
With no user, it uses your login name.
With no database, the server uses the one named after the user.

## Several hosts

Give several hosts, separated by commas, and fastpg tries them in order:

```python
dsn = "postgresql://app:secret@db1:5432,db2:5432/shop?target_session_attrs=read-write"
dsn = "host=db1,db2 port=5432,5433 user=app dbname=shop"
```

Each host goes with the port in the same place, or with the only port given.
fastpg moves on to the next host when one can't be reached, doesn't connect within
`connect_timeout`, or isn't what `target_session_attrs` asks for.
If none works, `ConnectError` lists what went wrong with each.

## TLS

The default, `sslmode=prefer`, encrypts the connection if the server offers TLS, but doesn't
check who the server is.
To check the server's certificate and host name, use `sslmode=verify-full`.

A Unix socket never uses TLS.
See [TLS](tls.md) for more.
