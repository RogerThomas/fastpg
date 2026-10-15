"""Named ``:name`` parameters: a ``str`` query plus ``params=`` (Struct, dataclass, model, mapping).

``"SELECT name FROM people WHERE id = :id AND age >= :min_age"`` with
``params={"id": 42, "min_age": 30}`` (or a Struct, dataclass or pydantic model with fields
``id`` and ``min_age``) runs as ``SELECT name FROM people WHERE id = $1 AND age >= $2`` with
parameters ``(42, 30)``.

Placeholders are found by a small scanner (cached per query string). ``:name`` becomes
``$n``, numbered by first appearance; a repeated name reuses its number. Not placeholders:
anything inside string literals (``'...'``, ``E'...'``), quoted identifiers, comments
(``--``, nested ``/* */``) and dollar quotes (``$$...$$``, ``$tag$...$tag$``); the ``::`` cast;
and a ``:`` not followed by a name. ``\\:`` is a literal ``:`` (for array slices).

Picking the values out of ``params=`` is cached per (query, params type): an
``operator.attrgetter`` (Structs, dataclasses, models) or ``itemgetter`` (mappings) over the
names, one C call per query.
"""

import dataclasses
import re
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from operator import attrgetter, itemgetter
from string.templatelib import Template
from typing import TYPE_CHECKING, Any, ClassVar

import msgspec

from fastpg.errors import ParameterError

if TYPE_CHECKING:
    from _typeshed import DataclassInstance
    from pydantic import BaseModel

type NamedParams = msgspec.Struct | DataclassInstance | BaseModel | Mapping[str, object]
"""What ``params=`` takes: values are read from fields (Structs, dataclasses, pydantic models,
by Python attribute name) or keys (mappings)."""

type _Getter = Callable[[Any], Any]

type _Bound = tuple[str, _Getter, bool]
"""A (query, params type)'s ``$n`` SQL, its values getter, and whether that returns a single
value (rather than a tuple)."""


def _no_values(_params: object) -> tuple[()]:
    """The getter for a query without placeholders."""
    return ()


class _Scanner:
    """Rewrites ``:name`` placeholders to ``$n`` (see the module docstring)."""

    # Where something other than plain SQL text may start. ``E'`` and ``$`` only count at the
    # start of a token: after a name character they are part of that name.
    _SPECIAL: ClassVar[re.Pattern[str]] = re.compile(r"(?<![\w$])[Ee]'|(?<![\w$])\$|\\:|['\"/:-]")
    _QUOTED: ClassVar[dict[str, re.Pattern[str]]] = {
        "'": re.compile(r"'[^']*(?:''[^']*)*'"),
        '"': re.compile(r'"[^"]*(?:""[^"]*)*"'),
        "E": re.compile(r"[Ee]'[^'\\]*(?:(?:\\.|'')[^'\\]*)*'", re.DOTALL),
        "e": re.compile(r"[Ee]'[^'\\]*(?:(?:\\.|'')[^'\\]*)*'", re.DOTALL),
    }
    _LINE_END: ClassVar[re.Pattern[str]] = re.compile(r"[\n\r]")
    _COMMENT_MARK: ClassVar[re.Pattern[str]] = re.compile(r"/\*|\*/")
    _DOLLAR_TAG: ClassVar[re.Pattern[str]] = re.compile(r"\$(?:[^\W\d]\w*)?\$")
    _NAME: ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

    @classmethod
    def _comment_end(cls, query: str, start: int) -> int:
        """End of the block comment at `start`; PostgreSQL nests them."""
        depth, pos = 1, start + 2
        while depth:
            mark = cls._COMMENT_MARK.search(query, pos)
            if mark is None:
                return len(query)
            depth += 1 if mark.group() == "/*" else -1
            pos = mark.end()
        return pos

    @classmethod
    def _dollar_end(cls, query: str, start: int) -> int:
        """End of the dollar-quoted string at `start` (`start` + 1 for a lone ``$``)."""
        if "0" <= query[start + 1 : start + 2] <= "9":
            msg = (
                f"query {query!r} mixes a $n placeholder with params=; use :name placeholders "
                "only (for literal text such as '$5', put it in a string literal)"
            )
            raise ValueError(msg)
        tag = cls._DOLLAR_TAG.match(query, start)
        if tag is None:
            return start + 1
        close = query.find(tag.group(), tag.end())
        return len(query) if close < 0 else close + len(tag.group())

    @classmethod
    def _skip(cls, query: str, start: int, char: str) -> int:
        """End of the literal, quoted identifier or comment at `start` (or `start` + 1)."""
        if char == "$":
            return cls._dollar_end(query, start)
        if char == "-":
            if not query.startswith("-", start + 1):
                return start + 1
            end = cls._LINE_END.search(query, start)
            return len(query) if end is None else end.end()
        if char == "/":
            return cls._comment_end(query, start) if query.startswith("*", start + 1) else start + 1
        quoted = cls._QUOTED[char].match(query, start)
        return len(query) if quoted is None else quoted.end()  # unterminated: the rest is quoted

    @classmethod
    def scan(cls, query: str) -> tuple[str, tuple[str, ...]]:
        """The ``$n`` SQL for `query`, and the placeholder names in ``$n`` order."""
        pieces: list[str] = []
        names: dict[str, int] = {}
        copied = pos = 0  # query[:copied] is in pieces
        while (special := cls._SPECIAL.search(query, pos)) is not None:
            start = special.start()
            char = query[start]
            if char == "\\":  # \: is a literal colon: drop the backslash, keep the colon
                pieces.append(query[copied:start])
                copied, pos = start + 1, start + 2
            elif char != ":":
                pos = cls._skip(query, start, char)
            elif query.startswith(":", start + 1):  # a :: cast
                pos = start + 2
            elif (name := cls._NAME.match(query, start + 1)) is None:
                pos = start + 1
            else:
                number = names.setdefault(name.group(), len(names) + 1)
                pieces.extend((query[copied:start], f"${number}"))
                copied = pos = name.end()
        pieces.append(query[copied:])
        return "".join(pieces), tuple(names)


@dataclass(slots=True, eq=False)
class NamedBinder:
    """Turns a ``:name`` query and its ``params=`` into (``$n`` SQL, parameter tuple).

    Two bounded caches (oldest entry evicted first, no reordering on hits, as in
    ``TemplateCompiler``): query → (SQL, names), and (query, params type) → `_Bound`.
    """

    # Getter factories, typed by what their getters return: always Any (a value or a tuple).
    _BY_KEY: ClassVar[Callable[..., _Getter]] = itemgetter
    _BY_ATTRIBUTE: ClassVar[Callable[..., _Getter]] = attrgetter

    max_size: int
    _scans: dict[str, tuple[str, tuple[str, ...]]] = field(
        init=False,
        default_factory=dict[str, tuple[str, tuple[str, ...]]],
    )
    _bound: dict[tuple[str, type[object]], _Bound] = field(
        init=False,
        default_factory=dict[tuple[str, type[object]], _Bound],
    )
    # Shared by every connection in the process, possibly on several threads' loops.
    # Lookups are single dict operations; only insert-with-eviction needs the lock.
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def _store[K, V](self, cache: dict[K, V], key: K, value: V) -> None:
        with self._lock:
            if len(cache) >= self.max_size:
                del cache[next(iter(cache))]
            cache[key] = value

    def _scan(self, query: str) -> tuple[str, tuple[str, ...]]:
        scanned = self._scans.get(query)
        if scanned is None:
            scanned = _Scanner.scan(query)
            self._store(self._scans, query, scanned)
        return scanned

    @staticmethod
    def _fields(params_type: type[object]) -> frozenset[str] | None:
        """The field names values are read from; None for a mapping (read by key)."""
        if issubclass(params_type, msgspec.Struct):
            return frozenset(params_type.__struct_fields__)
        if dataclasses.is_dataclass(params_type):
            return frozenset(f.name for f in dataclasses.fields(params_type))
        # Only consult pydantic if the application already imported it.
        if "pydantic" in sys.modules:
            from fastpg._pydantic import ModelPlanner

            if ModelPlanner.accepts(params_type):
                return frozenset(params_type.model_fields)
        if issubclass(params_type, Mapping):
            return None
        msg = (
            "params= must be a msgspec.Struct, a dataclass instance, a pydantic model or a "
            f"Mapping[str, object], got {params_type.__qualname__}"
        )
        raise TypeError(msg)

    @staticmethod
    def _missing(query: str, params_type: type[object], missing: list[str]) -> ParameterError:
        listed = ", ".join(f":{name}" for name in missing)
        msg = f"no value for {listed} in params= ({params_type.__qualname__}); query: {query!r}"
        return ParameterError(msg)

    def _bind_slow(self, query: str, params_type: type[object]) -> _Bound:
        rewritten, names = self._scan(query)
        fields = self._fields(params_type)
        getter: _Getter
        if not names:
            getter = _no_values
        elif fields is None:
            getter = self._BY_KEY(*names)
        else:
            missing = [name for name in names if name not in fields]
            if missing:
                raise self._missing(query, params_type, missing)
            getter = self._BY_ATTRIBUTE(*names)
        bound = (rewritten, getter, len(names) == 1)
        self._store(self._bound, (query, params_type), bound)
        return bound

    def bind(
        self,
        query: str | Template,
        args: tuple[object, ...],
        params: NamedParams,
    ) -> tuple[str, tuple[object, ...]]:
        """The ``$n`` SQL and parameter tuple for a query method called with ``params=``."""
        if type(query) is Template:
            msg = (
                "params= fills :name placeholders in a str query; a t-string takes its "
                "values from its interpolations"
            )
            raise TypeError(msg)
        if args:
            msg = (
                f"pass parameters positionally or as params=, not both; got {len(args)} "
                "positional parameter(s) and params="
            )
            raise TypeError(msg)
        params_type = type(params)
        bound = self._bound.get((query, params_type))
        if bound is None:
            bound = self._bind_slow(query, params_type)
        rewritten, getter, single = bound
        try:
            values = getter(params)
        except KeyError:
            if not isinstance(params, Mapping):
                raise
            missing = [name for name in self._scan(query)[1] if name not in params]
            raise self._missing(query, params_type, missing) from None
        return rewritten, (values,) if single else values
