"""t-string (PEP 750) queries: interpolations become bound parameters, never SQL text.

``t"SELECT name FROM people WHERE id = {pid} AND age >= {age}"`` runs as
``SELECT name FROM people WHERE id = $1 AND age >= $2`` with parameters ``(pid, age)``.

* A ``Template`` value is spliced in (its text inlined, its values renumbered), so query
  fragments compose: ``where = t"age >= {n}"; t"SELECT ... WHERE {where}"``.
* An ``Identifier`` value is spliced in as a double-quoted name (table, column, ...).
* Any other value becomes a ``$n`` parameter, encoded for the type the server declares.

The generated SQL is cached by the template's static parts. Within one t-string literal,
``Template.strings`` is the same tuple object on every evaluation (a code constant), so a dict
keyed by it hits on identity before any string comparison. Templates built at runtime miss
identity but still hit on equality.
"""

import re
import threading
from itertools import pairwise
from string.templatelib import Interpolation, Template
from typing import ClassVar, Final, Literal, final, override

type _Shape = tuple[tuple[str, ...], *tuple[_Shape | str | None, ...]]
"""A template's static structure: its strings, then per interpolation a nested template's
shape, an identifier's quoted text, or None for a parameter. It determines the SQL."""

type _Emitted = Literal["text", "param", "ident"]


@final
class Identifier:
    """A quoted SQL identifier, spliced into a t-string query's text.

    Parameters can only stand for values, so names (tables, columns, schemas, ...) that
    vary at runtime go through ``Identifier``:
    ``t"SELECT * FROM {Identifier('my schema', 'people')}"`` gives
    ``SELECT * FROM "my schema"."people"``. Each part is double-quoted with embedded quotes
    doubled, so it's taken verbatim: case-sensitive, never a keyword or SQL syntax.
    Put it where a bare name would go, never inside quotes.
    """

    __slots__ = ("_parts", "_quoted")

    def __init__(self, *parts: str) -> None:
        if not parts:
            msg = "Identifier() needs at least one name"
            raise ValueError(msg)
        for part in parts:
            if type(part) is not str:  # for untyped callers
                msg = f"Identifier parts must be str, got {type(part).__name__}"
                raise TypeError(msg)
            if not part or "\0" in part:
                msg = f"invalid SQL identifier {part!r}: must be non-empty and contain no NUL"
                raise ValueError(msg)
        self._parts = parts
        self._quoted = ".".join('"' + part.replace('"', '""') + '"' for part in parts)

    @property
    def parts(self) -> tuple[str, ...]:
        return self._parts

    @property
    def quoted(self) -> str:
        """The quoted form spliced into the query, e.g. ``"my schema"."people"``."""
        return self._quoted

    @override
    def __repr__(self) -> str:
        return f"Identifier({', '.join(map(repr, self._parts))})"

    @override
    def __eq__(self, other: object) -> bool:
        return isinstance(other, Identifier) and other._parts == self._parts

    @override
    def __hash__(self) -> int:
        return hash(self._parts)


def _check_interpolation(interpolation: Interpolation[object]) -> None:
    if interpolation.conversion is not None or interpolation.format_spec:
        spec = f"!{interpolation.conversion}" if interpolation.conversion else ""
        spec += f":{interpolation.format_spec}" if interpolation.format_spec else ""
        msg = (
            f"t-string query interpolation {{{interpolation.expression}{spec}}} has a "
            "conversion or format spec (the {x=} form implies !r); values are sent as "
            "parameters, never formatted into the SQL text"
        )
        raise ValueError(msg)


def _shape(template: Template, values: list[object]) -> _Shape:
    """`template`'s shape; appends its parameter values to `values` in order."""
    parts: list[_Shape | str | None] = []
    for interpolation in template.interpolations:
        _check_interpolation(interpolation)
        value = interpolation.value
        if type(value) is Template:
            parts.append(_shape(value, values))
        elif type(value) is Identifier:
            parts.append(value.quoted)
        else:
            values.append(value)
            parts.append(None)
    return (template.strings, *parts)


def _glues(char: str) -> bool:
    """Whether `char` next to a quoted identifier would merge with it or a quote around it."""
    return char.isalnum() or char in "_$\"'" or not char.isascii()


def _starts_with_digit(text: str) -> bool:
    return "0" <= text[:1] <= "9"


def _describe(last: _Emitted, segments: list[str]) -> str:
    """What precedes the next piece of output, for error messages."""
    if last == "param":
        return "a parameter"
    if last == "ident":
        return "an Identifier"
    return repr(segments[-1])


def _emit_text(text: str, segments: list[str], last: _Emitted) -> _Emitted:
    if not text:
        return last
    if _starts_with_digit(text) and (last == "param" or segments[-1].endswith("$")):
        msg = (
            f"t-string fragment {text!r} starts with a digit right after a parameter or '$'; "
            "it would read as a different $n placeholder"
        )
        raise ValueError(msg)
    if last == "ident" and _glues(text[0]):
        msg = f"an Identifier must stand alone as a name, but is followed directly by {text!r}"
        raise ValueError(msg)
    segments[-1] += text
    return "text"


def _emit_identifier(quoted: str, segments: list[str], last: _Emitted) -> _Emitted:
    tail = segments[-1][-1:]
    if last != "text" or (tail and _glues(tail)):
        msg = (
            f"Identifier {quoted} must stand alone as a name, but is preceded directly by "
            f"{_describe(last, segments)}"
        )
        raise ValueError(msg)
    segments[-1] += quoted
    return "ident"


def _emit_param(segments: list[str], last: _Emitted) -> _Emitted:
    if last == "ident" or segments[-1].endswith("$"):
        msg = f"a t-string parameter is preceded directly by {_describe(last, segments)}"
        raise ValueError(msg)
    segments.append("")
    return "param"


def _join(segments: tuple[str, ...] | list[str]) -> str:
    return segments[0] + "".join(f"${n}{text}" for n, text in enumerate(segments[1:], 1))


class TemplateCompiler:
    """Turns t-string templates into (``$n`` SQL, parameters), caching the SQL text.

    Two bounded caches (oldest entry evicted first, no reordering on hits, so a hit is a
    single dict lookup):

    * ``_flat``: ``template.strings`` → SQL, for templates whose values are all parameters.
      An entry also certifies that those strings passed validation.
    * ``_composed``: a template's shape (strings, nested shapes, identifier text) → SQL, for
      templates that splice other templates or identifiers. Identifier text is part of the
      key, because it is part of the SQL.

    Every check depends only on the key, so a template is accepted or rejected the same way
    whether or not its SQL is cached. Conversions and format specs aren't part of the static
    text (``t"{x!r}"`` and ``t"{x}"`` share one ``strings`` tuple), so they're checked on
    every call.
    """

    __slots__ = ("_composed", "_flat", "_lock", "_max_size")

    _placeholder: ClassVar[re.Pattern[str]] = re.compile(r"\$[0-9]")

    def __init__(self, max_size: int) -> None:
        self._flat: dict[tuple[str, ...], str] = {}
        self._composed: dict[_Shape, str] = {}
        self._max_size: Final = max_size
        # Shared by every connection in the process, possibly on several threads' loops.
        # Lookups are single dict operations; only insert-with-eviction needs the lock.
        self._lock: Final = threading.Lock()

    def _store[K](self, cache: dict[K, str], key: K, query: str) -> None:
        with self._lock:
            if len(cache) >= self._max_size:
                del cache[next(iter(cache))]
            cache[key] = query

    def _flat_sql(self, strings: tuple[str, ...]) -> str:
        """SQL for `strings` with every interpolation a parameter; validates on a miss."""
        query = self._flat.get(strings)
        if query is not None:
            return query
        for text in strings:
            if self._placeholder.search(text):
                msg = (
                    f"t-string query text {text!r} contains a literal $n placeholder; "
                    "t-strings number their own parameters, so interpolate {value} instead "
                    "(for literal text such as '$5', interpolate the whole string as a value)"
                )
                raise ValueError(msg)
        for before, after in pairwise(strings):
            if before.endswith("$"):
                msg = (
                    f"t-string query text {before!r} ends with '$' right before an "
                    "interpolation; it would read as a $n placeholder or a dollar quote"
                )
                raise ValueError(msg)
            if _starts_with_digit(after):
                msg = (
                    f"t-string query text {after!r} starts with a digit right after an "
                    "interpolation; it would change the $n placeholder number"
                )
                raise ValueError(msg)
        query = _join(strings)
        self._store(self._flat, strings, query)
        return query

    def _render(self, shape: _Shape, segments: list[str], last: _Emitted) -> _Emitted:
        """Append `shape`'s text to `segments` (a new segment per parameter).

        Besides each fragment's own checks (`_flat_sql`), rejects joins that would change how
        PostgreSQL tokenizes the result: a '$' + digit formed across fragments, a parameter
        glued to '$' or followed by a digit, and identifiers glued to name characters,
        quotes, parameters or other identifiers.
        """
        strings = shape[0]
        self._flat_sql(strings)
        last = _emit_text(strings[0], segments, last)
        for part, text in zip(shape[1:], strings[1:], strict=True):
            if part is None:
                last = _emit_param(segments, last)
            elif isinstance(part, str):
                last = _emit_identifier(part, segments, last)
            else:
                last = self._render(part, segments, last)
            last = _emit_text(text, segments, last)
        return last

    def _compile_slow(self, template: Template) -> tuple[str, tuple[object, ...]]:
        values: list[object] = []
        shape = _shape(template, values)
        if shape.count(None) == len(shape) - 1:  # every value is a parameter
            return self._flat_sql(template.strings), tuple(values)
        query = self._composed.get(shape)
        if query is None:
            segments = [""]
            self._render(shape, segments, "text")
            query = _join(segments)
            self._store(self._composed, shape, query)
        return query, tuple(values)

    def _compile(self, template: Template) -> tuple[str, tuple[object, ...]]:
        query = self._flat.get(template.strings)
        if query is not None:
            for interpolation in template.interpolations:
                value = interpolation.value
                if (
                    interpolation.conversion is not None
                    or interpolation.format_spec
                    or type(value) is Template
                    or type(value) is Identifier
                ):
                    break
            else:
                return query, template.values
        return self._compile_slow(template)

    def cache_len(self) -> int:
        """Number of cached SQL texts (both caches)."""
        return len(self._flat) + len(self._composed)

    def compile(self, template: Template) -> tuple[str, tuple[object, ...]]:
        """Return the ``$n`` SQL and the parameter tuple for `template`."""
        return self._compile(template)

    def bind(
        self,
        query: Template,
        params: tuple[object, ...],
    ) -> tuple[str, tuple[object, ...]]:
        """`compile` for a query method called with ``(query, *params)``."""
        if params:
            msg = (
                f"a t-string query takes its parameters from its interpolations; got "
                f"{len(params)} extra positional parameter(s)"
            )
            raise TypeError(msg)
        return self._compile(query)

    def script(self, template: Template) -> str:
        """SQL text for `execute_script`: only identifiers and fragments, no parameters."""
        query, params = self._compile(template)
        if params:
            msg = (
                "execute_script() uses the simple query protocol, which has no parameters; "
                f"the t-string has {len(params)} value interpolation(s). Use execute() "
                "(one statement), or Identifier for names"
            )
            raise TypeError(msg)
        return query
