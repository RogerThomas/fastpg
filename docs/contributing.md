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
