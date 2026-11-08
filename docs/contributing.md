# Contributing

To work on fastpg you need Docker, [uv](https://docs.astral.sh/uv/),
[Task](https://taskfile.dev) and a Rust toolchain.

```sh
task build    # build the extension into .venv
task test     # Rust tests and the pytest suite, in throwaway PostgreSQL containers
task check    # linters and type checks
task db-up    # the example database on 127.0.0.1:55432
task docs     # build and serve these docs
task --list   # everything else
```

## Pull requests

CI on a pull request runs the checks, and the tests on Python 3.14 on Linux and macOS.
The other Pythons, Linux aarch64, PostgreSQL 15 to 17 and the release wheels run on `main`,
or on a pull request with the `full-ci` label.
The wheels are also built when a pull request changes the packaging.

## Benchmarks

* The comparison with other drivers: see [Methodology](benchmarks.md#running-it).
* fastpg's own CPU cost per query is tracked on every pull request by
  [CodSpeed](https://app.codspeed.io/RogerThomas/fastpg), with the benchmarks in
  `benchmarks/`.
  To check they pass locally: `uv run pytest benchmarks`.

## Design

The [Architecture](architecture.md) page explains how fastpg works inside.
The decisions behind it are recorded as
[architecture decision records](https://github.com/RogerThomas/fastpg/blob/main/adr/README.md).
