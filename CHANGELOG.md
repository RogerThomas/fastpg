# Changelog

## [0.1.3](https://github.com/RogerThomas/fastpg/compare/v0.1.2...v0.1.3) (2026-10-23)


### Bug Fixes

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
