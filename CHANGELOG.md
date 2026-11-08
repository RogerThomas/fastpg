# Changelog

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
