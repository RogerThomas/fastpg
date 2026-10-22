"""``select_and_group``: rows grouped into Structs whose list fields collect one value per row.

Built entirely on ``select_many``: for each ``into=`` type ``T`` a flat *row* Struct is
derived once (cached), in which

* a field that isn't a list keeps its type, column, default and checks: these are the
  group key;
* a ``list[X]`` field becomes a field of type ``X`` reading the same column;
* a ``list[S]`` field, ``S`` a Struct, becomes one field per field of ``S``, reading the
  column ``<list column>__<field column>`` (``items__sku``), made ``| None`` so that a row
  without an element (a LEFT JOIN with no match: all of them NULL) decodes.

Rows are decoded into the row Struct by ``select_many`` (any strategy), then grouped in one
pass, keyed by the tuple of their key values, in the order each group's first row arrives.

A list field with a default (and each nested field with one) reads its column only when the
result has it: its row field defaults to ``_MISSING``, and a group leaves the field out,
so ``T``'s own default applies.
"""

import inspect
import itertools
import operator
import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import msgspec

from fastpg.errors import MissingColumnError, NullValueError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

# Aliases (evaluated lazily): annotations naming them work where annotations are evaluated
# eagerly (3.13) without those imports.
type _Getter = Callable[[Any], Any]
type _Values = Iterable[Any]


class _Missing:
    """The default of a row field whose column the result may leave out."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<missing column>"


_MISSING = _Missing()

_KEY = 0
_FLAT = 1
_NESTED = 2


def _list_item(annotation: object) -> object | None:
    """The item type of a ``list[X]`` (or bare ``list``) annotation, None for any other type.

    ``Annotated[list[X], ...]`` counts: its metadata (e.g. length constraints) applies to the
    list, which the grouping builds, not to a column.
    """
    if typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    if annotation is list:
        return Any
    if typing.get_origin(annotation) is list:
        (item,) = typing.get_args(annotation)
        return item
    return None


def _is_struct(annotation: object) -> typing.TypeIs[type[msgspec.Struct]]:
    return isinstance(annotation, type) and issubclass(annotation, msgspec.Struct)


def _takes_none(annotation: object) -> bool:
    """Whether NULL (None) is a valid value of `annotation` (``X | None``, ``Any``, ...)."""
    try:
        msgspec.convert(None, type=annotation)
    except msgspec.ValidationError:
        return False
    return True


def _name(cls: type[object]) -> str:
    """A class's name as fastpg's error messages give it (``f.<locals>.T`` → ``T``)."""
    return cls.__qualname__.rsplit("<locals>.", 1)[-1]


def _positional(cls: type[msgspec.Struct]) -> tuple[str, ...] | None:
    """The fields `cls` takes positionally, in order, if that's all of them, else None."""
    params = inspect.signature(cls).parameters.values()
    names = tuple(p.name for p in params if p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD)
    return names if len(names) == len(cls.__struct_fields__) else None


def _default(info: msgspec.structs.FieldInfo, column: str) -> Any:  # noqa: ANN401 - msgspec.field
    """The row field's ``msgspec.field``: `column`, and `info`'s default."""
    if info.default_factory is not msgspec.NODEFAULT:
        return msgspec.field(name=column, default_factory=info.default_factory)
    return msgspec.field(name=column, default=info.default)


@dataclass(frozen=True, slots=True)
class _Column:
    """A row field feeding a list field (`_FLAT`) or a field of its Structs (`_NESTED`)."""

    attr: str
    """The row Struct's attribute."""
    column: str
    target: str
    """The field it fills: of ``T`` (flat), or of the nested Struct."""
    required: bool = True
    """The field it fills has no default: a result without the column is an error."""
    nullable: bool = True
    """NULL is a valid value of the field it fills (checked here for nested fields only:
    the decoder checks the others)."""


@dataclass(frozen=True, slots=True)
class _Field:
    """One field of ``T``: a key field, a list field (`_FLAT`), or a list of Structs."""

    kind: int
    name: str
    has_default: bool
    columns: tuple[_Column, ...]
    """One for a key or flat field; one per field of `element` for a nested one."""
    element: type[msgspec.Struct] | None = None
    positional: tuple[str, ...] | None = None
    """`element`'s fields if it takes them all positionally (in that order)."""


class Grouping[T]:
    """How rows become grouped ``T``s: the row Struct to decode them into, and the grouping."""

    __slots__ = ("_fields", "_into", "_key", "_positional", "row")

    def __init__(self, into: type[T]) -> None:
        if not _is_struct(into):
            msg = (
                "select_and_group needs into= a msgspec.Struct subclass with at least one list"
                f" field, got {into!r}; use select_many for other row types"
            )
            raise TypeError(msg)
        self._into: type[Any] = into
        fields, row_fields = self._plan(into)
        if all(f.kind == _KEY for f in fields):
            msg = (
                f"select_and_group needs into= a Struct with at least one list field, and"
                f" {_name(into)} has none; use select_many to get one {_name(into)} per row"
            )
            raise TypeError(msg)
        self._fields = fields
        self._positional = _positional(into)
        keys = [f.name for f in fields if f.kind == _KEY]
        self._key: _Getter | None = operator.attrgetter(*keys) if keys else None
        config = into.__struct_config__
        self.row: type[msgspec.Struct] = msgspec.defstruct(
            into.__name__,
            row_fields,
            module=into.__module__,
            kw_only=True,
            forbid_unknown_fields=config.forbid_unknown_fields,
        )

    @staticmethod
    def _nested(
        into: type[msgspec.Struct],
        info: msgspec.structs.FieldInfo,
        element: type[msgspec.Struct],
    ) -> tuple[_Field, list[tuple[str, object, Any]]]:
        """A ``list[S]`` field: a row field per field of `S`, columns ``<list>__<field>``."""
        columns: list[_Column] = []
        row_fields: list[tuple[str, object, Any]] = []
        for sub in msgspec.structs.fields(element):
            item = _list_item(sub.type)
            if item is not None and _is_struct(item):
                msg = (
                    f"select_and_group can't group {_name(into)}.{info.name}: its items,"
                    f" {_name(element)}, have a list of Structs ({sub.name}), and nested"
                    " grouping isn't supported"
                )
                raise TypeError(msg)
            attr = f"{info.name}__{sub.name}"
            column = f"{info.encode_name}__{sub.encode_name}"
            # Required only when both the list and the item's field are: otherwise the result
            # may leave the column out (`Grouping._columns_of` checks what it then needs).
            optional = not (info.required and sub.required)
            default = msgspec.field(name=column, default=_MISSING) if optional else None
            row_fields.append((attr, sub.type | None, default or msgspec.field(name=column)))
            nullable = _takes_none(sub.type)
            columns.append(
                _Column(attr, column, sub.name, required=sub.required, nullable=nullable),
            )
        field = _Field(
            _NESTED,
            info.name,
            has_default=not info.required,
            columns=tuple(columns),
            element=element,
            positional=_positional(element),
        )
        return field, row_fields

    @classmethod
    def _plan(
        cls,
        into: type[msgspec.Struct],
    ) -> tuple[tuple[_Field, ...], list[tuple[str, object, Any]]]:
        """``T``'s fields, and the row Struct's (``defstruct`` fields)."""
        fields: list[_Field] = []
        row_fields: list[tuple[str, object, Any]] = []
        for info in msgspec.structs.fields(into):
            item = _list_item(info.type)
            column = info.encode_name
            if item is None:  # a key field: as it is
                fields.append(
                    _Field(
                        _KEY,
                        info.name,
                        has_default=not info.required,
                        columns=(_Column(info.name, column, info.name),),
                    ),
                )
                row_fields.append((info.name, info.type, _default(info, column)))
            elif _is_struct(item):
                field, nested = cls._nested(into, info, item)
                fields.append(field)
                row_fields += nested
            else:
                optional = not info.required
                default = msgspec.field(name=column, default=_MISSING) if optional else None
                row_fields.append((info.name, item, default or msgspec.field(name=column)))
                flat = _Column(info.name, column, info.name, required=info.required)
                fields.append(_Field(_FLAT, info.name, has_default=optional, columns=(flat,)))
        attrs = [name for name, _, _ in row_fields]
        if len(set(attrs)) != len(attrs):
            clash = next(a for a in attrs if attrs.count(a) > 1)
            msg = f"select_and_group can't group {_name(into)}: two fields both read {clash!r}"
            raise TypeError(msg)
        return tuple(fields), row_fields

    def _unhashable(self, rows: list[Any]) -> TypeError | None:
        """The error naming the first key value in `rows` that can't be hashed, if any."""
        for row in rows:
            for field in self._fields:
                if field.kind != _KEY:
                    continue
                value = getattr(row, field.name)
                try:
                    hash(value)
                except TypeError:
                    msg = (
                        f"select_and_group groups {_name(self._into)} rows by the fields that"
                        f" aren't lists, so their values must be hashable, but"
                        f" {_name(self._into)}.{field.name} is a {type(value).__name__}: make"
                        " it a list field, or leave it out of the Struct"
                    )
                    return TypeError(msg)
        return None

    def _columns_of(self, field: _Field, first: object) -> tuple[_Column, ...] | None:
        """The columns of list field `field` the result has (seen on its `first` row), or
        None when it has none and the field's default applies."""
        present = tuple(c for c in field.columns if getattr(first, c.attr) is not _MISSING)
        if not present and field.has_default:
            return None
        if not present:  # a nested list without a default, whose items have defaults only
            names = f"{field.name} (columns {field.name}__*)"
        else:  # the row Struct requires the rest, unless the list has a default
            missing = [c for c in field.columns if c.required and c not in present]
            if not missing:
                return present
            names = ", ".join(f'{c.attr} (column "{c.column}")' for c in missing)
        msg = f"{_name(self._into)} requires field(s) {names} but the query does not return them"
        raise MissingColumnError(msg)

    @staticmethod
    def _nested_builder(field: _Field, columns: tuple[_Column, ...]) -> _Getter:
        """``members -> list`` for a nested list field: an item per row, from `columns`,
        except where they're all NULL."""
        attrs = [c.attr for c in columns]
        many = operator.attrgetter(*attrs)
        get: Callable[[Any], tuple[Any, ...]] = many if len(attrs) > 1 else lambda r: (many(r),)
        element: Any = field.element
        targets = tuple(c.target for c in columns)

        def by_name(*values: object) -> object:
            return element(**dict(zip(targets, values, strict=True)))

        # Positionally if that's every field, in order.
        make: Callable[..., object] = element if field.positional == targets else by_name

        nones = (None,) * len(columns)
        strict = [(i, c) for i, c in enumerate(columns) if not c.nullable]

        def check(values: tuple[Any, ...]) -> None:
            for i, c in strict:
                if values[i] is None:
                    msg = (
                        f'column "{c.column}" is NULL but {_name(element)}.{c.target}'
                        " is not optional"
                    )
                    raise NullValueError(msg)

        def build(members: list[Any]) -> list[Any]:
            # A row whose columns are all NULL has no item (e.g. a LEFT JOIN without a match).
            rows = [values for values in map(get, members) if values != nones]
            if strict:
                for values in rows:
                    if None in values:
                        check(values)
            return list(itertools.starmap(make, rows))

        return build

    def _groups(self, rows: list[Any]) -> dict[Any, list[Any]]:
        """`rows` by key (in the order of each key's first row): one pass, a dict lookup each."""
        key = self._key
        if key is None:  # no key fields: one group
            return {(): rows}
        groups: dict[Any, list[Any]] = {}
        get = groups.get
        try:
            for k, row in zip(map(key, rows), rows, strict=True):
                members = get(k)
                if members is None:
                    groups[k] = [row]
                else:
                    members.append(row)
        except TypeError:
            error = self._unhashable(rows)
            if error is None:
                raise
            raise error from None
        return groups

    def _columns(self, groups: dict[Any, list[Any]], first: object) -> list[tuple[str, _Values]]:
        """Each field's value for every group, in group order, as ``(field, values)``.

        By field, not by group, so that per group nothing runs in Python but a list field's
        ``list(map(getter, rows))`` (and a nested one's builder). A list field the result has
        no columns for (seen on its `first` row) is left out: ``T``'s default fills it.
        """
        keys = groups.keys()
        members = groups.values()
        single_key = sum(f.kind == _KEY for f in self._fields) == 1
        columns: list[tuple[str, _Values]] = []
        index = 0  # in the key tuple
        for field in self._fields:
            if field.kind == _KEY:
                # A group's key is its values (the one value, for a single key field).
                values = keys if single_key else map(operator.itemgetter(index), keys)
                columns.append((field.name, values))
                index += 1
            elif (present := self._columns_of(field, first)) is None:
                continue
            elif field.kind == _FLAT:
                get = operator.attrgetter(present[0].attr)
                columns.append((field.name, [list(map(get, m)) for m in members]))
            else:
                columns.append((field.name, map(self._nested_builder(field, present), members)))
        return columns

    def group(self, rows: list[Any]) -> list[T]:
        """Group `rows` (row Structs) into ``T``s: one per distinct key, in first-row order."""
        if not rows:
            return []
        columns = self._columns(self._groups(rows), rows[0])
        into = self._into
        if self._positional == tuple(name for name, _ in columns):  # all of them, in order
            # The columns are equally long (no strict=: that's 3.14+).
            return list(map(into, *(values for _, values in columns)))  # noqa: B912
        names = [name for name, _ in columns]
        return [
            into(**dict(zip(names, values, strict=True)))
            for values in zip(*(values for _, values in columns), strict=True)
        ]


_groupings: dict[type[Any], Grouping[Any]] = {}


def grouping_for[T](into: type[T]) -> Grouping[T]:
    """The cached `Grouping` for `into`; TypeError if it can't be grouped."""
    grouping = _groupings.get(into)  # absent: first use of this type
    if grouping is None:
        grouping = _groupings[into] = Grouping(into)
    return grouping
