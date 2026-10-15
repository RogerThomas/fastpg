# 0003. Minimal runtime dependencies

- Status: Accepted
- Date: 2026-10-10

## Context

The project was scaffolded from a generic Python template that declared `httpx` and
`rich` as runtime dependencies and a `fastpg = "fastpg:main"` console script backed by a
hello-world `main()`.

## Decision

- `httpx` is removed: nothing uses it.
- `rich` moves to the dev dependency group; only tooling and benchmarks use it.
- The console script entry and the placeholder `main()` are removed.

## Consequences

The installed package pulls in only what the library needs at runtime. Any of these can be
re-added in `pyproject.toml` if a real use appears.
