# Changelog

## [0.2.3](https://github.com/RogerThomas/fastpg/compare/v0.2.2...v0.2.3) (2026-11-12)


### Features

* LISTEN/NOTIFY: listen() on pools and connections, notify() on pools, connections and transactions ([#97](https://github.com/RogerThomas/fastpg/issues/97)) ([83aefde](https://github.com/RogerThomas/fastpg/commit/83aefdefdcbc0fd6be8d3fc989946e98cc3dc647))

## [0.2.2](https://github.com/RogerThomas/fastpg/compare/v0.2.1...v0.2.2) (2026-11-10)


### Features

* time out connecting and waiting for a pool connection after 30 seconds by default ([#90](https://github.com/RogerThomas/fastpg/issues/90)) ([438bf26](https://github.com/RogerThomas/fastpg/commit/438bf266939b9b26f070fbcef8de951f82cb9bd4))


### Bug Fixes

* **errors:** map every SQLSTATE class to an error class, as psycopg does ([#85](https://github.com/RogerThomas/fastpg/issues/85)) ([2b870d9](https://github.com/RogerThomas/fastpg/commit/2b870d9e3cfcaf2ec24c93760694bb7c5940bb31))
* **pool:** refuse a connection used after its acquire() block ends ([#91](https://github.com/RogerThomas/fastpg/issues/91)) ([f5f668f](https://github.com/RogerThomas/fastpg/commit/f5f668f4c0cc19e63ebc3d68b4d0f6d5b3e7623d))
* refuse to use a connection from another event loop, before sending anything ([#92](https://github.com/RogerThomas/fastpg/issues/92)) ([cf9360e](https://github.com/RogerThomas/fastpg/commit/cf9360e176d36fa408a478809b4400fa4275a1ba))


### Performance Improvements

* send uuid columns as 16-byte bin on the msgpack route when the field is a UUID ([#94](https://github.com/RogerThomas/fastpg/issues/94)) ([eb332da](https://github.com/RogerThomas/fastpg/commit/eb332da7adf13ca08eed0ef6903a4fe2d1dda01c))

## [0.2.1](https://github.com/RogerThomas/fastpg/compare/v0.2.0...v0.2.1) (2026-11-10)


### Bug Fixes

* **release:** publish to PyPI again when a release is created ([#77](https://github.com/RogerThomas/fastpg/issues/77)) ([3bd2001](https://github.com/RogerThomas/fastpg/commit/3bd20018cb0fe5ba84e44d88b37f3c13d1af3726))

## [0.2.0](https://github.com/RogerThomas/fastpg/compare/v0.1.4...v0.2.0) (2026-11-09)


### ⚠ BREAKING CHANGES

* a json or jsonb query parameter now takes what bulk writes take. Values that used to be refused are now stored: a `tuple` as a JSON array, `bytes` as base64 text, a `bool`, number, `UUID` or date as its JSON. An `Enum` whose value is a `str` is now stored as a JSON string, as bulk writes did, not sent as JSON text.
* `Pool(...)` and `Transaction(...)` can no longer be called directly; use `await fastpg.create_pool(dsn, ...)` and `conn.transaction()` / `pool.transaction()`. `DatabaseError.from_server` is now private (`_from_server`), and `fastpg.errors.SQLStateRegistry` is now private (`_SQLStateRegistry`).
* `statement_cache_size=0` used to be treated as 1. It now raises `ValueError` on `connect()` and `create_pool()`.
* connect failures and closed or lost connections are `OperationalError` (`ConnectError`, `ConnectionClosedError`), no longer `InterfaceError`. `DatabaseError.sqlstate` and `.severity` are `str | None`.

### Features

* connection failures are OperationalError (ConnectError, AuthenticationError) ([#54](https://github.com/RogerThomas/fastpg/issues/54)) ([49fe053](https://github.com/RogerThomas/fastpg/commit/49fe0530027f6ebbac8e848db684469496ccd3fe))
* json and jsonb parameters accept what bulk writes accept ([#63](https://github.com/RogerThomas/fastpg/issues/63)) ([06d234b](https://github.com/RogerThomas/fastpg/commit/06d234b35becfe5dd06af2ff5ed83dafa68e605a))
* read PG* environment variables and .pgpass, like libpq ([#55](https://github.com/RogerThomas/fastpg/issues/55)) ([1f1b674](https://github.com/RogerThomas/fastpg/commit/1f1b674ada3e8458197c033e8c88b500710afe20))
* type query strings as LiteralString so type checkers catch f-string SQL ([#65](https://github.com/RogerThomas/fastpg/issues/65)) ([bfcc6da](https://github.com/RogerThomas/fastpg/commit/bfcc6da34d15bb56f3fb891a569e9689b6085385))
* with no host or user, connect like libpq: local socket and OS user name ([#66](https://github.com/RogerThomas/fastpg/issues/66)) ([8e8592b](https://github.com/RogerThomas/fastpg/commit/8e8592b1d9732d47a03fac61c150b7c8db48ea2c))


### Bug Fixes

* a bulk write cancelled while BEGIN runs commits nothing ([#58](https://github.com/RogerThomas/fastpg/issues/58)) ([013932b](https://github.com/RogerThomas/fastpg/commit/013932bd365f0fc0cc7334a59417e00cff96e33e))
* a FATAL error ends the session even when the server's close is lost ([#56](https://github.com/RogerThomas/fastpg/issues/56)) ([62cc007](https://github.com/RogerThomas/fastpg/commit/62cc00707efba1826d20882051aded18b21f58a6))
* refuse statement_cache_size=0 on connect() and create_pool() ([#61](https://github.com/RogerThomas/fastpg/issues/61)) ([5bd0f0d](https://github.com/RogerThomas/fastpg/commit/5bd0f0df90c60917542560bc351d4caa385b5c13))
* strategy="auto" keeps Decimal digits inside JSON fields ([#57](https://github.com/RogerThomas/fastpg/issues/57)) ([e478dc4](https://github.com/RogerThomas/fastpg/commit/e478dc41deb6b58a5754c2f4365c68df19adc48e))


### Performance Improvements

* build the release wheels with profile-guided optimisation ([#75](https://github.com/RogerThomas/fastpg/issues/75)) ([9be771f](https://github.com/RogerThomas/fastpg/commit/9be771f941168d433506f975daf4237dd6535017))
* use mimalloc in the musl wheels ([#74](https://github.com/RogerThomas/fastpg/issues/74)) ([3c2f668](https://github.com/RogerThomas/fastpg/commit/3c2f6687e61bd555b11111348e4bd31fc6b43b62))
* write UUIDs, dates and numbers without format! in the msgpack and JSON routes ([#68](https://github.com/RogerThomas/fastpg/issues/68)) ([b31efa8](https://github.com/RogerThomas/fastpg/commit/b31efa89d492c48c00b884e6a4f5012e8f09a6d7))


### Code Refactoring

* hide Pool and Transaction constructors, DatabaseError.from_server and SQLStateRegistry ([#64](https://github.com/RogerThomas/fastpg/issues/64)) ([f32a5bd](https://github.com/RogerThomas/fastpg/commit/f32a5bd9c593b55650e9ca74c8d09ab4d9ed1c45))

## [0.1.4](https://github.com/RogerThomas/fastpg/compare/v0.1.3...v0.1.4) (2026-11-08)


### Bug Fixes

* COPY through a query method is a clear error and keeps the connection ([#47](https://github.com/RogerThomas/fastpg/issues/47)) ([f136dce](https://github.com/RogerThomas/fastpg/commit/f136dce1153e9f3e879c8b54959a66d49a1a78fe))
* **deps:** update rustls-webpki, webpki-roots and ctutils for RustSec advisories ([#40](https://github.com/RogerThomas/fastpg/issues/40)) ([1f1b514](https://github.com/RogerThomas/fastpg/commit/1f1b514e2cdf937032e16f22d5af279fc5c9015c))
* FETCH from a redeclared cursor decodes its new column types ([#41](https://github.com/RogerThomas/fastpg/issues/41)) ([711201b](https://github.com/RogerThomas/fastpg/commit/711201b584902f90bb47e8806bdf9d894981aecc))
* JSON values read through msgpack match the other strategies exactly ([#44](https://github.com/RogerThomas/fastpg/issues/44)) ([2249d12](https://github.com/RogerThomas/fastpg/commit/2249d12cf352203adee5860ca233acded1d79408))
* multiple hosts pair with their ports and fall through on target_session_attrs, like libpq ([#43](https://github.com/RogerThomas/fastpg/issues/43)) ([b1d955a](https://github.com/RogerThomas/fastpg/commit/b1d955a78d872b72d9c33f6e81cfb31c945d2693))
* **pool:** don't show the password in repr(pool) ([#38](https://github.com/RogerThomas/fastpg/issues/38)) ([8872750](https://github.com/RogerThomas/fastpg/commit/88727502bfee895acee0be2af55e0e6433d4281b))


### Performance Improvements

* end a fetch chunk at 1 MiB as well as at 512 rows ([#46](https://github.com/RogerThomas/fastpg/issues/46)) ([65c6ea7](https://github.com/RogerThomas/fastpg/commit/65c6ea771e5c40c404321c45ad839fbdb65289a8))
* find a free task slot in constant time ([#45](https://github.com/RogerThomas/fastpg/issues/45)) ([241eae0](https://github.com/RogerThomas/fastpg/commit/241eae05b214840ee8b2d24365bd3311f5e5c648))
* jsonb parameters with None are nearly as fast as before [#27](https://github.com/RogerThomas/fastpg/issues/27) ([#48](https://github.com/RogerThomas/fastpg/issues/48)) ([e09ef36](https://github.com/RogerThomas/fastpg/commit/e09ef36d7b99862c3c3551c6d54afb5cb1f9f138))

## [0.1.3](https://github.com/RogerThomas/fastpg/compare/v0.1.2...v0.1.3) (2026-10-30)


### Bug Fixes

* **pool:** max_idle counts time asleep and is checked when a connection is picked ([#32](https://github.com/RogerThomas/fastpg/issues/32)) ([7f8fbd1](https://github.com/RogerThomas/fastpg/commit/7f8fbd1e1def4bf83ec4d719ca0276f385ec0255))
* refuse NaN and infinities in JSON parameters instead of storing null ([#27](https://github.com/RogerThomas/fastpg/issues/27)) ([2424c7b](https://github.com/RogerThomas/fastpg/commit/2424c7b8af4ac5ccd8a530abd3d049e2052d2547))

## [0.1.2](https://github.com/RogerThomas/fastpg/compare/v0.1.1...v0.1.2) (2026-10-22)


### Bug Fixes

* a NULL in a non-optional field is a NullValueError under msgpack and json too ([#24](https://github.com/RogerThomas/fastpg/issues/24)) ([2e1ee67](https://github.com/RogerThomas/fastpg/commit/2e1ee67172388c4ae10d13c7c2ae8754e5c84b4b))

## [0.1.1](https://github.com/RogerThomas/fastpg/compare/v0.1.0...v0.1.1) (2026-10-22)


### Features

* PostgreSQL arrays: read them into lists, send lists as arrays ([#19](https://github.com/RogerThomas/fastpg/issues/19)) ([10449ff](https://github.com/RogerThomas/fastpg/commit/10449ff2c663cd8a1754d88476ded137163d3a0e))
* support Python 3.13, 3.14 and 3.15 (and free-threaded 3.14t, 3.15t) ([#18](https://github.com/RogerThomas/fastpg/issues/18)) ([4fb9549](https://github.com/RogerThomas/fastpg/commit/4fb9549214f6e51a93e8faf8c6136364280588c5))

## 0.1.0 (2026-10-22)


### Features

* CI, wheels for Linux and macOS, and PyPI releases ([f4272b7](https://github.com/RogerThomas/fastpg/commit/f4272b7ea7a0ad94fe00773c860fdcbd82816d0a))


### Bug Fixes

* cancelling a bulk write inside a transaction no longer aborts the transaction ([#14](https://github.com/RogerThomas/fastpg/issues/14)) ([8c14788](https://github.com/RogerThomas/fastpg/commit/8c1478890c772269edfa32bdd1bad6e904ec31a0))


### Performance Improvements

* cross-driver benchmark suite run in GitHub Actions ([10a01c5](https://github.com/RogerThomas/fastpg/commit/10a01c524c6b60190d5a0d1c168637431bbcb010))
