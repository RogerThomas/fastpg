# t-string queries (PEP 750)

Queries can be written as Python 3.14 template strings. Interpolations become **bound
parameters** and never become SQL text:

```python
person = await cur.select_one(
    t"SELECT name, age FROM people WHERE id = {sel.id} AND age >= {sel.min_age}",
    into=Person,
)
# runs: SELECT name, age FROM people WHERE id = $1 AND age >= $2   with (sel.id, sel.min_age)

await cur.execute(t"UPDATE people SET email = {email} WHERE id = {pid}")
```

Code: `src/fastpg/_template.py` (pure Python). Tests: `tests/test_templates.py`. Benchmark:
`bench/template_overhead.py`.

## API

* `Connection.select_one / select_one_or_none / select_many / execute` take `str | Template`
  (so do `Transaction`'s and `Pool`'s).
  Overloads are unchanged otherwise, so `into=Person` still types as `Person`,
  `Person | None` and `list[Person]`.
  - A template plus extra positional `*params` raises `TypeError`, because the parameters
    come from the interpolations.
* `Connection.execute_script` also takes a `Template`, but only one that produces **no
  parameters**: no interpolations, or only `Identifier`s and parameter-free fragments.
  Otherwise it raises `TypeError` (see Decisions).
* **Composition:** an interpolated `Template` is spliced in. Its text is inlined and its
  values are renumbered:
  ```python
  between = t"id BETWEEN {low} AND {high}"
  where = t"{between} AND age >= {min_age}"
  await cur.select_many(t"SELECT id FROM people WHERE {where} ORDER BY id LIMIT {n}")
  # SELECT id FROM people WHERE id BETWEEN $1 AND $2 AND age >= $3 ORDER BY id LIMIT $4
  ```
* **`fastpg.Identifier(*parts)`** splices a double-quoted name, with embedded `"` doubled:
  `t"SELECT * FROM {Identifier('my schema', 'people')}"` becomes
  `SELECT * FROM "my schema"."people"`. Use it for tables, columns, schemas and type
  names (`{v}::{Identifier('int4')}`); parameters can't stand for any of these.
  - It rejects zero parts, empty parts, NUL and non-`str` parts.
  - It has `.parts`, `.sql`, `==`/`hash` and a `repr`.
* **Escape hatch for trusted dynamic SQL text:** `Template("ORDER BY created_at DESC")`. A
  `str` argument to the `Template` constructor is static text. That's explicit, and greppable.
* **Errors** are builtin types, because they're programming errors at the call site:
  - `TypeError` for the wrong call shape: extra `*params`, or values in `execute_script`;
  - `ValueError` for an invalid template: a conversion or format spec, a literal `$n`, a
    glued placeholder or identifier, or a bad `Identifier`.

## Decisions and why

1. **Values are always bound parameters.** There's no client-side literal quoting at all.
   The server sees the same `$n` SQL as hand-written code would, so t-string queries share
   the per-connection prepared-statement cache with equivalent `$n` queries (tested with
   `statement_cache_len()`). They also get the existing strict, server-type-directed
   parameter encoding unchanged.
2. **Conversions (`!r`/`!s`/`!a`) and format specs (`:...`) are rejected.** They imply text
   formatting, and silently ignoring them would be surprising. `{x=}` is rejected too,
   because it implies `!r`.
3. **Literal `$<digit>` in template text is rejected, conservatively.** No SQL lexer is
   involved: `re.search(r"\$[0-9]")` runs over each static string. It also rejects the joins
   that would silently change a placeholder number:
   - `${x}`: `$` before an interpolation would give `$$1`;
   - `{x}1`: a digit after an interpolation would give `$11`;
   - a fragment ending in `$` or starting with a digit next to a parameter.

   False positives, such as `$5` inside a string literal or a comment, have a clean
   workaround: interpolate the text as a value (`{'costs $5'}`). Dollar quotes (`$$`,
   `$tag$`) aren't affected.
4. **`Identifier` was implemented (the optional item).** Identifier text changes the SQL, so
   it's part of the cache key (see below). It must stand alone as a name: it may not be
   directly preceded or followed by a name character, a `$`, a quote, a parameter or
   another identifier.
   - This catches the f-string habits `'{ident}'`, `"{ident}"`, `tbl_{ident}`.
   - It also catches `{a}{b}`. Two quoted identifiers glued together lex as one name
     containing a `"`.
   - The quote checks matter. With `'{ident}'`, a `'` inside the identifier would end the
     string literal. With `"prefix_{ident}"`, the identifier's opening quote would close the
     user's quoted name, and the content would be lexed as SQL.
5. **`execute_script` accepts parameter-free templates rather than rejecting templates.**
   - DDL can't take bind parameters in PostgreSQL, so dynamic table names in DDL scripts
     (setup, migrations, per-tenant schemas) are exactly where `Identifier` is needed, and
     scripts are where DDL lives.
   - Templates with value interpolations are rejected, not quoted client-side (decision 1).
     The error points to `execute()` for single statements.
   - The `$n` rule applies to scripts too, for consistency. Function bodies that use `$1`
     need a plain `str` script.
6. **Typing.** `query: str | Template` in the existing overloads keeps the diff to
   `_connection.py` small: 3 `isinstance` branches and the signature changes.
   - Not done: separate `Template`-only overloads without `*params`. They would turn
     "template plus params" into a static error, but double the overload count. That's
     easy to add if wanted.
7. **One process-wide compiler** (`_Queries._templates`, a class attribute of the base
   that `Connection` and `Transaction` share). The generated SQL doesn't depend on the
   connection, so every connection and transaction share one cache (the pool has its own).
8. **Pure Python, not native.** A cached call costs 0.14 to 0.2 µs, and end to end it's
   below the noise of the loop-CPU measurement (see below). A Rust version could save maybe
   ~0.1 µs per query and would duplicate the validation logic, so it isn't justified.

## Cache design

**Finding (CPython 3.14.7):** `template.strings` is the **same tuple object** on every
evaluation of one t-string literal, because it's a code-object constant. A dict keyed by it
hits on identity, since dict lookup compares with `is` before `==` and str hashes are cached.
Templates built at runtime (`Template(...)`, `+`) produce equal but distinct tuples, which
still hit on equality at about the same cost (+5 ns measured).

**Caveat found while probing:** the compiler deduplicates equal constants within a code object.
So `t"a {x!r} b"` and `t"a {x} b"` in the same function share one `strings` object. Identity
of `strings` therefore does **not** imply identical conversions or format specs. These are
checked on every call, in the same loop that checks for spliced values.

Two caches on `TemplateCompiler`:

| cache | key | used when |
|---|---|---|
| `_flat` | `template.strings` | every value is a plain parameter (the hot path) |
| `_composed` | the **shape**: `(strings, *parts)`, where each part is a nested shape, an identifier's quoted text, or `None` for a parameter | any value is a `Template` or `Identifier` |

* **Hot path:** one `dict.get(template.strings)`, then one loop over `template.interpolations`
  checking conversion, format spec, and `type(value) is Template/Identifier`. On a hit it
  returns `(sql, template.values)`.
* **Composed path:** one recursive walk builds the shape and collects values in order. The
  shape is the key, so different fragments or identifiers in the same outer literal get
  different SQL (tested). The hit path does no string building.
* **Validation depends only on the key.** A template is accepted or rejected identically
  whether or not it's cached. A `_flat` entry also certifies that its strings passed the
  text checks; fragments are validated through it too.
* **Bounded:** each cache holds at most 1024 entries.
  - When full, the oldest entry is evicted (FIFO via dict insertion order). Hits don't
    reorder entries, so a hit stays a single lookup.
  - A miss costs ~1.5 µs, so occasional re-insertion of a hot entry is harmless.
  - Insertion takes a `threading.Lock`, because several threads may each run a loop. Lookups
    are lock-free.
  - `cache_len()` exposes the size for the boundedness test.

## Measured overhead

Apple M3 Max, CPython 3.14.7, best of 7 `timeit` repeats:

```
uv run yeet ./bench/template_overhead.py
```

| scenario | per call |
|---|---:|
| `str` query: the new `isinstance(query, Template)` dispatch | 31 ns |
| cached t-string, 1 parameter | 143 ns |
| cached t-string, 3 parameters | 204 ns |
| same SQL from a template rebuilt at runtime (equality hit), 3 parameters | 209 ns |
| composed: fragment + `Identifier`, 3 parameters | 686 ns |
| cache miss (fresh static text every call), 1 parameter | 1.5 µs |
| for reference: building the 3-parameter t-string itself (paid at the call site) | 96 ns |

The target was well under 1 µs for a cached template with a few interpolations. The
measured cost is 0.14 to 0.2 µs.

End to end, event-loop-thread CPU per sequential point lookup into a Struct (uvloop, live
DB):

```
FASTPG_DSN=postgresql://fastpg:fastpg@127.0.0.1:55432/fastpg_tstr \
  uv run yeet ./bench/template_overhead.py end_to_end
```

* Results: `$1` + param 7.0 µs vs t-string 7.4 µs.
* Over several runs, the difference ranged from +0.4 to +0.8 µs, which is about the
  run-to-run noise of this harness.
* An interleaved scratch run with a prebuilt template, so no t-string construction in the
  loop, cost the same as building it per call. So the extra above the ~0.2 µs
  microbenchmark isn't attributable to the template machinery itself.

## Limitations

* **Arrays aren't supported as parameters yet** (see `params.rs`). A `list` is encoded as JSON,
  so `= ANY({ids})` fails with `ParameterError: cannot encode dict/list (JSON) as PostgreSQL
  type _int4` (tested). Workaround (tested):
  `WHERE id IN (SELECT jsonb_array_elements_text({ids})::int)`. Once arrays land,
  `= ANY({ids})` will just work.
* **No list-join helper** (like psycopg's `sql.SQL(", ").join(...)`). Building
  `IN ({a}, {b}, ...)` from a variable-length list needs the JSON workaround above, or
  manual `Template` concatenation.
* **`$n` detection is conservative** (decision 3). It rejects `$5` inside string literals and
  comments, `$1` in function bodies, and unquoted identifiers such as `price$1`.
* **`Identifier` adjacency checks are local.** An identifier placed inside a quoted string
  or a quoted name *with whitespace around it* (`'note {ident} here'`) isn't detected. Rule:
  put `Identifier` where a bare name goes, never inside quotes. This is the same contract as
  psycopg's `sql.Identifier`.
* `{x=:}` (debug form with an explicit empty spec) has no conversion, so it's accepted. Its
  `x=` text becomes part of the SQL.
* Repeated values (`{x} ... {x}`) become separate parameters; they aren't deduplicated.
* PostgreSQL's limit of 65535 parameters per statement applies; the server reports it.
* `Identifier` subclasses aren't recognised; it's `@final`. At runtime a subclass instance
  would be sent as a parameter and fail with `ParameterError`.

## Open questions

1. Separate `Template` overloads, so passing extra `*params` with a template is a type error?
   (Decision 6.)
2. Should `execute_many` (bulk-writes branch) accept a template? Each row has different
   values, so the natural shape is one template *factory* per row, or a template whose
   values are placeholders. This is left to the merge.
3. The pool (`_pool.py`) delegates to `Cursor`. Its own signatures need `str | Template`
   for the type checker to accept t-strings there.
