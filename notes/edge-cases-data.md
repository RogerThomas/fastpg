# Edge cases: when the data surprises the client

What fastpg does when the values, the schema or the parameters aren't what the code
expects, for every result target: tuples, `dict`, `NamedTuple`, msgspec Structs (`direct`,
`msgpack`, `json`) and pydantic models (`direct`, `json`), through `select_one`,
`select_many`, `execute` and parameters.

Branch `worktree-agent-a7aed0d1698871165`, from `main` at `62f748a`. Probed with scripts
against a throwaway `postgres:18.6-trixie` container, then pinned down as tests:

| file | case |
|---|---|
| `tests/test_edge_nulls.py` | 1. NULL where the type says it can't be |
| `tests/test_edge_values.py` | 2. values Python can't represent |
| `tests/test_edge_schema_changes.py` | 3. schema changes under cached statements |
| `tests/test_edge_types.py` | 4. unsupported and user-defined types |
| `tests/test_edge_mid_result_errors.py` | 5. errors after some rows |
| `tests/test_edge_huge.py` | 6. huge values, wide rows, a million rows |
| `tests/test_edge_naming.py` | 7. column and field naming |
| `tests/test_edge_params.py` | 8. parameter surprises |

Open issues are strict `xfail`s, so each one fails loudly (`XPASS`) once fixed.

## Summary

| case | behaviour | bug? | fixed? | test |
|---|---|---|---|---|
| NULL → non-optional field, direct | `NullValueError` naming column and field, every target | no | | `test_null_into_non_optional_field_names_column_and_field` |
| NULL → non-optional field, msgpack/json | `DecodeError` from the validator (`Expected int, got None - at $[999].value`) | inconsistent class | open (O1) | `..._is_a_null_value_error_under_every_strategy` (xfail) |
| NULL → `Any` field, direct | was `NullValueError`; msgspec/pydantic accept None | yes | `0e0f69e` | `test_any_field_takes_null_under_every_strategy` |
| NULL in row 1000 of 5000 | whole `select_many` fails, no partial list, connection fine | no | | `test_null_in_row_1000_of_5000_fails_the_whole_select_many` |
| `infinity`, year ≥ 10000, BC dates/timestamps | `DecodeError` naming the column, every target | no | | `test_date_python_cannot_represent_is_a_decode_error` |
| `time '24:00:00'` | `DecodeError` (cause `ValueError: hour must be in 0..23`) | no | | `test_time_2400_is_a_decode_error` |
| numeric 131072 digits / scale 16383 | exact under every strategy, both ways | no | | `test_numeric_digits_survive_exactly` |
| numeric NaN/±Inf → pydantic `Decimal`, direct | was `Decimal('NaN')`; validation refuses it | yes | `db90399` | `test_non_finite_numeric_into_a_pydantic_decimal_is_refused` |
| float8 NaN/±Inf → Struct, `json` | was raw `msgspec.DecodeError` "JSON is malformed" | yes | `fc2d03d` | `test_non_finite_float8_has_no_json_form_for_msgspec` |
| float8 `-0.0`, NaN, ±Inf otherwise | preserved | no | | `test_negative_zero_float8_keeps_its_sign`, `test_non_finite_float8` |
| int8 extremes → int / float field | exact / rounded like `float(int)` | no | | `test_int8_extremes` |
| timestamptz under odd `TimeZone` | same UTC instant (binary format) | no | | `test_timestamptz_is_the_same_instant_...` |
| invalid UTF-8 text | `DecodeError` naming the column; server only sends it to a non-UTF8 client | no | | `test_invalid_utf8_...` |
| `client_encoding` ≠ UTF8 | parameters and query text silently reinterpreted | yes | open (O2) | `test_client_encoding_other_than_utf8_...` (xfail) |
| NUL in text | server refuses (`54000`, `22021`); reaches Python only via `json` | no | | `test_text_holding_nul_is_refused_by_the_server` |
| column added/dropped/retyped/renamed, table recreated, view redefined, `DEALLOCATE ALL`, pool | re-prepared and rebound, every target | no | | `test_edge_schema_changes.py` |
| stale statement inside a transaction | was 25P02 "transaction is aborted", hiding the cause | yes | `9682bd1` | `test_stale_statement_in_a_transaction_reports_the_schema_change` |
| parameter column retyped under a cached statement | was broken for good on that connection | yes | `d94aab1` | `test_parameter_retyped_under_a_cached_statement*` |
| arrays, interval, ranges, composites, hstore, inet, money, xml, bit, ... | `UnsupportedTypeError` naming column and type, before any row, also for no rows | no | | `test_unsupported_type_names_the_column_and_type` |
| unsupported column no field reads, msgpack/json | fails the query (direct ignores it) | inconsistent | open (O3) | `..._ignored_through_validation` (xfail) |
| domain parameters | were refused (`cannot encode int as ... posint`) | yes | `faf4b3d` | `test_domain_parameters_are_encoded_as_their_base_type` |
| domain results, enum labels added at runtime | decode as base type / text | no | | `test_domains_decode_...`, `test_enum_label_added_...` |
| error after N rows (`1/(i-1000)`) | `DataError`, no partial result, connection usable, tx aborted | no | | `test_edge_mid_result_errors.py` |
| 100 MB text/bytea, 200 MB row, 1 GB-1 value | work; peak RSS ≈ 3× the value | no | | `test_100_mb_text_and_bytea`, `test_200_mb_row` |
| connection memory after a huge value | keeps a receive buffer ≈ 1.3× its largest message until closed | yes (memory) | open (O4) | measured below |
| 1,600-column table, 1,664-column select list | work, every strategy | no | | `test_1600_columns`, `test_most_columns_a_select_list_takes` |
| 1M rows `select_many` | 0.22 s; Python peak = result size | no | | `test_million_rows_peak_memory_stays_near_the_result_size` |
| duplicate column for a field, msgpack/json | silently kept the last value | yes | `1e767e5` | `test_duplicate_column_for_a_field_is_refused_by_every_target` |
| pydantic `populate_by_name` / `validate_by_alias=False` | direct matched aliases only, unlike validation | yes | `010cfc4` | `test_pydantic_matches_columns_as_its_validation_does` |
| missing renamed field | message didn't name the column needed | misleading | `f318e32` | `test_column_name_beyond_63_bytes_is_truncated_by_the_server` |
| missing column, empty result, msgpack/json | returns `[]` (direct raises) | inconsistent | open (O3) | `..._empty_result_through_validation` (xfail) |
| wrong Python type, naive/aware, None, bool, counts | `ParameterError` naming parameter and types; bool refused for numbers | no | | `test_edge_params.py` |
| int beyond 64 bits → numeric/float | was refused ("does not fit in 64 bits") | yes | `caf4919` | `test_int_beyond_64_bits_for_numeric` |
| float beyond float4 range | was sent as `Infinity` / `0.0` | yes | `678657e` | `test_float_beyond_float4_range_is_out_of_range` |
| unencodable str / JSON value | raw `UnicodeEncodeError`/`TypeError` leaked | yes | `82d1b5a` | `test_unencodable_python_value_is_a_parameter_error` |
| > 65535 parameters | misleading `InterfaceError`, statement left on the server | yes | `2104853` | `test_more_than_65535_parameters_are_refused_before_preparing` |
| Decimal NaN/sNaN/-NaN/±Inf | sent as numeric NaN/±Infinity | no | | `test_decimal_special_values` |

## 1. NULL where the type says it can't be

* **Direct** (Struct, NamedTuple, pydantic): `NullValueError('column "value" is NULL but
  Value.value is not optional')`, naming the class and *attribute* (so `rename=` and aliases
  show the Python name). Raised when the row is decoded; the connection stays usable.
* **`select_many`, NULL at row 1,000 of 5,000**: rows arrive in 512-row chunks and the first
  is decoded while the rest streams; the failing chunk sets the future's exception, later
  chunks are skipped, and no partial list is ever returned. Tuples and dicts just hold `None`.
* **msgpack/json**: the encoder writes `nil`/`null` and msgspec or pydantic rejects it:
  `DecodeError('Expected `int`, got `None` - at `$[999].value`')` (row index included, but
  not `NullValueError`). An existing test (`test_null_into_non_optional_via_msgpack_is_decode_error`)
  pins this; open issue O1.
* **`Any` fields** (fixed, `0e0f69e`): direct construction treated `Any` as non-nullable
  and raised `NullValueError`, while msgspec and pydantic validation take `None` for `Any`,
  so `auto` (direct) disagreed with `msgpack`/`json`. `Any` is now nullable.
* `execute` with `None` into a `NOT NULL` column: `NotNullViolationError` from the server.

## 2. Values Python can't represent

| value | tuple / dict / direct | msgpack / json |
|---|---|---|
| `date`/`timestamp`/`timestamptz` `±infinity` | `DecodeError: column "value" (date): infinite dates cannot be represented in Python` | same text without the type |
| year ≥ 10000, BC (`0044-03-15 BC` is year -43) | `DecodeError: ... year -43 is out of range for Python dates` | same |
| `time '24:00:00'` | `DecodeError: failed to decode column "value" (time)`, cause `ValueError` | msgspec/pydantic "invalid time" `DecodeError` |
| numeric NaN/±Infinity | `Decimal('NaN')` etc. | Struct: same; pydantic `Decimal`: `DecodeError` "finite number" |
| numeric, 131072 digits or scale 16383 | exact (`Decimal(str)`) | exact |
| float8 NaN/±Inf | `nan`/`inf` | msgpack: same; json: pydantic same, msgspec `DecodeError` naming the column |
| float8 `-0.0` | `-0.0` | `-0.0` |
| int8 `±2**63` → `float` field | `float(int)` rounding | same |

* `timestamptz` decodes to an aware UTC `datetime` whatever the session `TimeZone`
  (`Asia/Kathmandu`, `America/St_Johns`, `Pacific/Kiritimati`, `Etc/GMT+12`, a POSIX spec);
  `DateStyle` is irrelevant too (binary format). Parameters likewise.
* `interval`: `UnsupportedTypeError` (case 4).
* **Text.** PostgreSQL refuses NUL in text both ways (`chr(0)` → 54000, a `"a\x00b"`
  parameter → 22021); only a `json` (not `jsonb`) value with `\u0000` brings one into
  Python. Invalid UTF-8 reaches the client only when the client asks for a non-UTF8
  encoding: an `SQL_ASCII` database still validates text sent to a UTF8 client (22021), but
  with `SET client_encoding = 'SQL_ASCII'` (or `LATIN1`, where `chr(233)` is the lone byte
  0xe9) the bytes arrive as they are and every target raises `DecodeError` naming the column
  (direct: cause `UnicodeDecodeError`). The connection stays usable.
* **Fixed** (`db90399`): pydantic's `Decimal` must be finite unless
  `Field(allow_inf_nan=True)`, so `json` raised for numeric NaN/±Infinity while `direct` —
  what `auto` picks for such a model — returned `Decimal('NaN')`. Direct construction now
  raises `DecodeError` naming the column, the field and `allow_inf_nan`.
* **Fixed** (`fc2d03d`): under `strategy="json"` a msgspec Struct fed a non-finite float
  failed with `msgspec.DecodeError: JSON is malformed: invalid character (byte 5)` — not a
  fastpg exception, no column named. JSON has no form of NaN that msgspec accepts (the
  encoder writes the `NaN` tokens pydantic takes); for msgspec the encoder now raises
  `DecodeError('column "value": float8 NaN has no JSON form msgspec accepts; use
  strategy="direct" or "msgpack"')`, and msgspec plans catch `msgspec.DecodeError` (the
  base of `ValidationError`) so nothing unwrapped can escape.

## 3. Schema changes under cached prepared statements

`SELECT * FROM t` cached for every target, then: a column added, dropped, retyped
(`int` → `text`), two columns of one type swapping names, the table dropped and recreated
with columns reordered, a view dropped and recreated with its columns swapped (and
`CREATE OR REPLACE` adding one), `DEALLOCATE ALL`, and a pool of four connections all
holding the statement. In every case the server reports the cached plan stale (0A000
"cached plan must not change result type", or 26000), fastpg re-prepares and retries once,
and the new `CachedStmt` gets fresh codecs and fresh column→field bindings (the `bindings`
cache in `stmt_cache.rs` is per statement, so a stale binding can't be reused). Values
follow the new names; a retyped column raises `TypeMismatchError` under direct, `DecodeError`
under msgpack/json, and pydantic `json` coerces `"1"` to `1` (lax validation).

* **Fixed** (`9682bd1`): inside a transaction the 0A000 error has already aborted the
  transaction, so the re-prepare failed with 25P02 "current transaction is aborted" and the
  real cause was lost. The original `NotSupportedError` is now raised (documented in
  `docs/guide/transactions.md`).
* **Fixed** (`d94aab1`): a cached statement keeps the parameter types it was prepared with.
  After `ALTER COLUMN x TYPE text`, `WHERE x = $1` was broken on that connection (and each
  pooled one) until LRU eviction: a `str` failed to encode for the old `int4`, an `int`
  failed on the server ("operator does not exist: text = integer", 42883). An encoding error
  on a cached statement now re-prepares and retries once (nothing was sent); 42883/42804 on a
  cached statement evicts it, so the next call prepares afresh (not retried: those codes can
  also come from executing the statement, e.g. inside a function).

## 4. Unsupported types

`int4[]`, `text[]`, `interval`, `int4range`, `tstzrange`, `record`, a composite type,
`hstore`, `inet`, `cidr`, `money`, `xml`, `bit`, `varbit`, `timetz`, `macaddr`, `point`,
`tsvector`, `regclass`: `UnsupportedTypeError('column "value" has PostgreSQL type _int4,
which fastpg cannot decode')` for every target, raised before any row is decoded, also for
an empty result, and for tuples the whole query fails. (Arrays show PostgreSQL's internal
name, `_int4`.) As parameters: `ParameterError('parameter $1: cannot encode str as
PostgreSQL type interval')`; a `list` is JSON, so `int4[]` gets "cannot encode dict/list
(JSON)". Workaround for both: cast in SQL (`$1::text::interval`, `col::text`).

* Domains decode as their base type everywhere (RowDescription reports the base type).
* **Fixed** (`faf4b3d`): a parameter typed as a domain (`INSERT` into a domain column,
  `$1::posint`) was refused, because ParameterDescription reports the domain's OID. Bulk
  writes already unwrapped domains; query parameters now share that helper, and errors name
  both (`positive (a domain over int4)`). Domain `CHECK` failures are `CheckViolationError`.
* Enums: labels added with `ALTER TYPE ... ADD VALUE` while statements are cached read and
  write fine (fastpg sends labels as text and never checks them); an unknown label is the
  server's `DataError`.
* `citext`, `name`, `oid` decode.
* Open (O3): an unsupported column that no field reads is ignored by direct construction
  but fails the query under msgpack/json, which encode every column.

## 5. Errors in the middle of a result

`SELECT 1/(i - N) FROM generate_series(1, 5000)` with N = 500 (first chunk), 1000 (after
one 512-row chunk was decoded) and 4000: `DataError('division by zero (SQLSTATE 22012)')`
for every target and strategy, never a partial list; the stream is drained to
ReadyForQuery, so the next query works and queries pipelined behind it succeed.
`select_one`, `select_one_or_none` and `execute` likewise. In a transaction the error aborts
it: the next statement raises 25P02, the block's commit raises the documented
`DatabaseError` 25P02, and the earlier writes are rolled back.

## 6. Huge values

macOS, release build, against the throwaway container:

| value | time | peak RSS | after the value is dropped |
|---|---|---|---|
| 100 MB text | 0.6 s | 339 MB | |
| 500 MB text | 3.4 s | 1,537 MB | |
| 1 GB - 1 text | 7.4 s | 3,106 MB | |
| 200 MB text | | | connection still holds a 263 MB block (`heap`) until closed |

* Peak ≈ 3× the value: xitca's receive buffer, fastpg's row arena (one copy), the Python
  object. Nothing leaks: everything is freed when the connection closes (checked with
  `heap(1)`; macOS RSS itself doesn't shrink). But while the connection lives, xitca keeps
  its receive buffer at the size of the largest message (open issue O4).
* 100 MB parameters (text, bytea) and a 200 MB row of two values work. The arena's 32-bit
  offsets limit a result to 4 GiB (a clear `InterfaceError` beyond).
* 1,600 columns (PostgreSQL's table maximum) into tuples, dicts and a 1,600-field Struct
  under every strategy, positionally or by keyword (columns reordered); 1,664 (the
  select-list maximum) × 1,000 rows in 0.14 s; 1,665 is the server's error 54011.
* 1M rows of `(int, text)` into a Struct: 0.22 s, RSS +154 MB for 103-129 MB of Python
  objects; tracemalloc's peak equals the final result size (chunks are decoded while the
  rest streams, so raw rows never pile up). Tuples: 0.22 s.

## 7. Column and field naming

* Duplicate column names: tuples keep both; `dict`, Structs, NamedTuples and models raise
  `TypeMismatchError` for a duplicate a field reads (an unread duplicate is ignored).
  **Fixed** (`1e767e5`): msgpack/json encoded both under one key and the decoder silently
  kept the last value; those routes now get every field's column name and raise the same
  error, also for an empty result.
* Case: unquoted `AS Name` folds to `name`; a field `Name` needs `AS "Name"`.
* Spaces, apostrophes, unicode and (for dicts) quotes and backslashes in column names work
  under every strategy; msgspec itself refuses `"` and `\` in renamed names. JSON keys are
  escaped once per statement.
* Identifiers are truncated to 63 bytes by the server, so a longer rename never matches.
  **Fixed** (`f318e32`): the `MissingColumnError` named only the attribute (`requires
  field(s) value`); a renamed or aliased field now names the column it needs (`value
  (column "aaaa...")`).
* Extra columns: ignored by default; `forbid_unknown_fields=True` / `extra="forbid"` raise
  `UnknownColumnError` (direct) or `DecodeError` (validation).
* Missing columns: a default fills in under every strategy; without one, direct raises
  `MissingColumnError` even for an empty result, validation a `DecodeError` per row — and
  nothing for an empty result (open, O3).
* **Fixed** (`010cfc4`): with `populate_by_name` / `validate_by_name`, pydantic validation
  also matches a column named after the field (with `validate_by_alias=False`, only that);
  direct construction matched aliases only, so `auto` raised `MissingColumnError` where
  validation accepts the column, or accepted one validation refuses. Such models now take
  the `json` route.

## 8. Parameter surprises

* Wrong Python type: `ParameterError('parameter $2: cannot encode str as PostgreSQL type
  int4')` before anything is sent; the connection stays usable. `bool` is refused for every
  number type although it is an `int` subclass; a naive datetime for `timestamptz` and an
  aware one for `timestamp` have their own messages.
* `None` is SQL NULL: `WHERE x = $1` matches nothing, `IS NOT DISTINCT FROM $1` works.
* Too few / too many: `ParameterError('query expects 2 parameter(s), got 1')`.
* Ranges: int2/int4/oid edges exact, one past them `int out of range for PostgreSQL type
  int4`.
* Decimal `NaN`, `-NaN`, `sNaN` → numeric NaN, `±Infinity` → numeric ±Infinity; `NaN123`
  (payload) is refused; a scale beyond 16383 is the server's `DataError`.
* NUL: refused by the server in text (22021) and jsonb; fine in `json` and `bytea`.
* NaN/±Inf inside a dict/list parameter become JSON `null` (msgspec's encoding), silently.
* **Fixed** (`caf4919`): ints beyond 64 bits were refused even for numeric (documented as
  taking `int`) and float parameters; they now go as their decimal digits, and integer
  types say "int out of range" (also in bulk writes).
* **Fixed** (`678657e`): a float beyond float4's range was sent as `Infinity` (1e300) or
  `0.0` (1e-50); PostgreSQL's own cast refuses both, and so does fastpg now (queries and
  bulk writes).
* **Fixed** (`82d1b5a`): a Python error while converting a value (a lone surrogate, a dict
  holding something msgspec can't serialise) leaked as a raw `UnicodeEncodeError` /
  `TypeError` / `RecursionError`; it is now `ParameterError('parameter $1: ...')` caused by
  it, as bulk writes already did.
* **Fixed** (`2104853`): with more than 65,535 values the server prepared the statement
  but described its parameters with a wrapped 16-bit count, so the query failed with
  `InterfaceError: invalid message length: parameters is not drained` and left the
  statement on the server. Exactly 65,535 works; more is refused before preparing.

## Open issues and proposed fixes

* **O1. NULL through validation isn't a `NullValueError`.** msgpack/json leave NULLs to
  msgspec/pydantic, so catching `NullValueError` works for some types and not others
  (`auto` picks msgpack for constrained Structs). Proposed: give the Plan the columns of
  fields whose type refuses `None` (decided once per type, e.g. `msgspec.convert(None,
  type=T)` / `TypeAdapter(T).validate_python(None)` failing) and have the encoders raise the
  direct message when such a column is NULL. The existing test pinning `DecodeError` keeps
  passing (subclass).
* **O2. `client_encoding` other than UTF8 corrupts text silently.** fastpg always sends
  UTF-8; after `SET client_encoding = 'LATIN1'` the server reinterprets parameters and query
  text (`length($1)` of `"é"` is 2, stored as `Ã©`), and results come back mostly
  re-encoded so the round trip looks fine. Statements prepared meanwhile keep literals parsed
  under the wrong encoding after a `RESET`. Proposed: track the `client_encoding`
  ParameterStatus the server sends on change (xitca would need to surface it) and fail
  queries with `InterfaceError` while it isn't UTF8; at least document it next to the
  pool's "don't change session state".
* **O3. The msgpack/json routes don't bind columns before decoding.** Consequences: an
  empty result never reports a missing required column; an unsupported-type column no field
  reads fails the query; type mismatches surface per row, as validation errors. Proposed:
  build a names-only `Binding` for those routes (every field `Kind::Any`, with
  required/default information), once per (statement, type) like the direct binding, and
  encode only the bound columns.
* **O4. A connection keeps a receive buffer the size of its largest message.** After one
  1 GB value a pooled connection holds ~1.3 GB until it is replaced (`max_lifetime`,
  default an hour). Proposed: shrink xitca's read buffer after a large message (upstream),
  or let the pool retire a connection after a result above a threshold.
* **O5. `Any` fields differ by route.** Direct gives the column's Python type (`Decimal`,
  `datetime`, `UUID`); msgpack/json give their text (`"NaN"`, ISO strings), because that is
  what those encodings carry. By design, but undocumented.
* **O6. NaN in a JSON parameter becomes `null`.** msgspec's choice; fastpg could refuse it
  (`ParameterError`), as it refuses other values that don't survive.
* **O7. 42883/42804 after a column type change fail once.** The re-prepare happens on the
  next call. A transparent retry needs to know the error came from Bind (re-planning), not
  from executing the statement, which xitca doesn't expose.
* **O8. Minor messages.** BC years show astronomically (`year -43` for 44 BC); msgpack/json
  decode errors omit the column type that direct ones include; a query with more than
  65,535 `$n` placeholders but fewer values still gets xitca's "parameters is not drained".
