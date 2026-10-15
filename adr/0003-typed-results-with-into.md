# 0003. Typed results with into=

## Context

fastpg's main purpose is to return rows as typed objects without intermediate dicts or
tuples, with the same results msgspec or pydantic validation would give.

## Decision

- Without `into=`, rows are tuples. With it, rows become msgspec Structs or pydantic v2
  models; pydantic is optional and only touched if the application imported it.
- Columns bind to fields by encoded name (`rename=`, `alias`), and column types are checked
  against annotations once per (statement, type), before any row is decoded, even for
  empty results. NULL in a non-optional field raises `NullValueError`.
- `strategy=` picks the decode mode:
  - `direct`: Rust decodes each value and builds the instance, Structs through `__init__`
    by vectorcall (defaults and `__post_init__` apply), models as `model_construct` does.
  - `msgpack` (Structs only) and `json`: Rust encodes rows, and a cached msgspec decoder or
    pydantic `validate_json` builds and validates them.
  - `auto` (default): `direct` when provably equivalent to validation (no `Meta`
    constraints, enums, `Literal` or non-optional unions; for models, no validators,
    constraints or mutable defaults), otherwise `msgpack` for Structs and `json` for models.
- `into=dict` (keyed by column name; duplicate names raise rather than drop a value) and
  `into=` a `NamedTuple` (bound like a Struct, called positionally) are built directly
  only; `msgpack` and `json` raise `UnsupportedTypeError` for them.
- A json/jsonb column feeding a `str` field gives its raw text in every mode.
- Tuples, Structs and NamedTuples holding only scalars, and UUID and Decimal values, are
  untracked by the cyclic GC. Dict rows stay tracked: CPython 3.14 wouldn't re-track one
  given a container later.
- `select_many` decodes in 512-row chunks while later rows arrive.

## Consequences

Validation is never silently skipped; `strategy="direct"` raises `UnsupportedTypeError`
when it would be. Chunked decoding made 10,000-row fetches faster than asyncpg.
