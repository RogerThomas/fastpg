"""Row-target planning: what the native decoder needs for an `into=` type.

Targets are msgspec Structs, `NamedTuple`s, pydantic models and `dict`. Each target type is
inspected once (cached) and yields a `Plan`:

* `spec`: the native spec for **direct** construction, or None when the type can't be built
  directly (with the reason in `direct_error`).
* decoders for the **msgpack** route (msgspec only) and the **json** route (both), where
  Rust encodes rows and a native validating parser (msgspec / pydantic-core) builds the
  objects.

For msgspec Structs a field is either a plain scalar (optionally `| None`) that Rust builds
directly, a JSON-shaped type (list, dict, nested Struct, ...) decoded from a json/jsonb
column with a cached `msgspec.json.Decoder(<annotation>)`, or something only msgspec
validation produces correctly (`Meta` constraints, enums, literals, non-optional unions,
tz-constrained datetimes). Such Structs can't be built directly; strategy "auto" routes them
through msgpack instead. Pydantic models are analysed in `fastpg._pydantic`.

`NamedTuple` fields are classified like Struct fields. `dict` rows have no fields: every
column is a key. Both are only built directly: the msgpack/json routes encode rows as maps,
which msgspec decodes into a NamedTuple only from arrays, and a dict built from them would
lose the column types (dates as strings, ...).
"""

import inspect
import sys
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, ClassVar, Literal, TypeIs

import msgspec
import msgspec.inspect as mi

from fastpg._native import StructSpec


class Kind(IntEnum):
    """Field kinds understood by the native decoder (order must match Rust `FieldKind`)."""

    ANY = 0
    BOOL = 1
    INT = 2
    FLOAT = 3
    STR = 4
    BYTES = 5
    DATETIME = 6
    DATE = 7
    TIME = 8
    UUID = 9
    DECIMAL = 10
    JSON = 11


type FieldArg = tuple[str, str, int, bool, bool, object | None]
"""`(attr_name, encode_name, kind, nullable, required, json_decoder)` for `StructSpec`."""


class NeedsValidationError(Exception):
    """A field can only be produced correctly by the library's own validation."""


@dataclass(frozen=True, slots=True)
class Plan[T]:
    """Everything needed to produce `T` from rows, by every strategy."""

    spec: StructSpec | None
    """Native spec for direct construction, or None (see `direct_error`)."""
    direct_error: str | None
    fallback: Literal["msgpack", "json"]
    """Strategy "auto" uses when direct construction is impossible."""
    msgpack_one: Callable[[bytes], T] | None
    msgpack_many: Callable[[bytes], list[T]] | None
    msgpack_error: str | None
    json_one: Callable[[bytes], T] | None
    json_many: Callable[[bytes], list[T]] | None
    json_error: str | None
    json_non_finite_floats: bool
    """The JSON decoders take `NaN`/`Infinity` tokens (pydantic's do, msgspec's don't: a
    non-finite float then fails the query, naming its column)."""
    validation_error: type[Exception]
    """Raised by the validating decoders; re-raised as `fastpg.DecodeError`."""
    text_columns: tuple[str, ...]
    """Columns feeding `str` fields: the msgpack/json routes encode a json/jsonb value there
    as its text, which is what direct construction gives such a field (ADR 0003)."""
    field_columns: tuple[str, ...]
    """The column of every field: the msgpack/json routes refuse a result naming one twice,
    as direct construction does (a decoder would keep one value and drop the other)."""


class StructPlanner:
    """Builds `Plan`s for msgspec Structs and NamedTuples (see the module docstring)."""

    _SCALARS: ClassVar[dict[type[mi.Type], Kind]] = {
        mi.AnyType: Kind.ANY,
        mi.BoolType: Kind.BOOL,
        mi.IntType: Kind.INT,
        mi.FloatType: Kind.FLOAT,
        mi.StrType: Kind.STR,
        mi.BytesType: Kind.BYTES,
        mi.DateTimeType: Kind.DATETIME,
        mi.DateType: Kind.DATE,
        mi.TimeType: Kind.TIME,
        mi.UUIDType: Kind.UUID,
        mi.DecimalType: Kind.DECIMAL,
    }
    _JSON_SHAPED: ClassVar[tuple[type[mi.Type], ...]] = (
        mi.ListType,
        mi.TupleType,
        mi.VarTupleType,
        mi.SetType,
        mi.FrozenSetType,
        mi.DictType,
        mi.StructType,
        mi.TypedDictType,
        mi.DataclassType,
        mi.NamedTupleType,
    )

    @staticmethod
    def _is_str(t: mi.Type) -> bool:
        """Whether a field type is `str` or `str | None` (constraints don't matter)."""
        if isinstance(t, mi.UnionType):
            members = [m for m in t.types if not isinstance(m, mi.NoneType)]
            return len(members) == 1 and isinstance(members[0], mi.StrType)
        return isinstance(t, mi.StrType)

    @classmethod
    def _classify(cls, t: mi.Type) -> tuple[Kind, bool]:
        """Return `(kind, nullable)` for a msgspec field type, or raise NeedsValidationError."""
        nullable = False
        if isinstance(t, mi.UnionType):
            members = [m for m in t.types if not isinstance(m, mi.NoneType)]
            if len(members) != 1:
                msg = "unions other than `T | None` need msgspec validation"
                raise NeedsValidationError(msg)
            nullable = len(members) != len(t.types)
            t = members[0]
        kind = cls._SCALARS.get(type(t))  # absent: not a scalar
        if kind is not None:
            # For msgspec's scalar inspect types, every field is a constraint (ge, max_length,
            # pattern, tz, ...). Direct construction would silently skip them.
            constraints = {
                name: value
                for name in t.__struct_fields__
                if (value := getattr(t, name)) is not None
            }
            if constraints:
                msg = f"constraints {constraints} need msgspec validation"
                raise NeedsValidationError(msg)
            # `Any` takes None, as msgspec validation does: NULL is no error there.
            return kind, nullable or kind is Kind.ANY
        if isinstance(t, cls._JSON_SHAPED):
            return Kind.JSON, nullable
        msg = f"{type(t).__name__} needs msgspec validation"
        raise NeedsValidationError(msg)

    @classmethod
    def _field_args(
        cls,
        owner: type[object],
        info_fields: tuple[mi.Field, ...],
        annotations: dict[str, Any],
        attr_names: dict[str, str],
    ) -> tuple[list[FieldArg], str | None]:
        """`StructSpec` fields, or why direct construction is unavailable."""
        fields: list[FieldArg] = []
        for field in info_fields:
            try:
                kind, nullable = cls._classify(field.type)
            except NeedsValidationError as e:
                return fields, f"{owner.__qualname__}.{field.name}: {e}"
            decoder: Any = None
            if kind is Kind.JSON:
                decoder = msgspec.json.Decoder(annotations[field.name]).decode
            attr = attr_names[field.name]
            fields.append((attr, field.encode_name, kind, nullable, field.required, decoder))
        return fields, None

    @staticmethod
    def _untrackable_named_tuple(target: type[tuple[Any, ...]]) -> bool:
        """Whether instances can only reference their items: no `__dict__`, and no class
        between `target` and the namedtuple class (whose `_fields` it defines, and whose
        `__new__` typing.NamedTuple doesn't let a class body replace) overrides `__new__`,
        which could store other values than the arguments."""
        if target.__dictoffset__ != 0:
            return False
        for klass in target.__mro__:
            if "_fields" in klass.__dict__:
                return True
            if "__new__" in klass.__dict__:
                return False
        return False

    @staticmethod
    def _allocatable_named_tuple(target: type[tuple[Any, ...]], arity: int) -> bool:
        """Whether calling `target` with `arity` positional arguments does no more than
        `tuple.__new__(target, args)`, so the native builder may allocate instances and fill
        their items itself. That holds when:

        * the metaclass is `type`, whose `__call__` calls `__new__`, then `__init__`;
        * `__new__` is the one `collections.namedtuple` generates for `arity` fields,
          `lambda _cls, a, b, ...: _tuple_new(_cls, (a, b, ...))` with `_tuple_new` being
          `tuple.__new__`: the namedtuple class's own, which a subclass may inherit but not
          override (its bytecode is compared with that of a namedtuple of the same arity);
        * `__init__` is `object.__init__`, which ignores the arguments;
        * instances have no `__dict__`. A subclass adding methods keeps the namedtuple's
          layout when it declares `__slots__ = ()` (without it, instances get a `__dict__`;
          a tuple subclass can't declare non-empty slots).
        """
        if type(target) is not type or target.__init__ is not object.__init__:
            return False
        if target.__dictoffset__ != 0:
            return False
        new = target.__new__
        probe_fields: list[tuple[str, type[object]]] = [(f"f{i}", object) for i in range(arity)]
        probe: type[tuple[Any, ...]] = typing.NamedTuple("Probe", probe_fields)
        reference = probe.__new__
        if not isinstance(new, types.FunctionType) or not isinstance(reference, types.FunctionType):
            return False
        # `vars(tuple)["__new__"]` is `tuple.__new__`, without its generic signature.
        tuple_new = vars(tuple)["__new__"]
        if new.__globals__.get("_tuple_new") is not tuple_new:  # absent: not generated
            return False
        code, expected = new.__code__, reference.__code__
        return (
            code.co_code == expected.co_code
            and code.co_names == expected.co_names
            and code.co_argcount == expected.co_argcount
            and code.co_kwonlyargcount == expected.co_kwonlyargcount
            and code.co_flags == expected.co_flags
        )

    @classmethod
    def plan(cls, struct: type[msgspec.Struct]) -> Plan[msgspec.Struct]:
        info = mi.type_info(struct)
        if not isinstance(info, mi.StructType):  # guaranteed for Struct types
            msg = f"msgspec does not describe {struct!r} as a Struct"
            raise TypeError(msg)
        annotations = {f.name: f.type for f in msgspec.structs.fields(struct)}
        attr_names = {name: name for name in struct.__struct_fields__}  # interned names
        fields, direct_error = cls._field_args(struct, info.fields, annotations, attr_names)
        # kw_only fields can't be passed positionally; msgspec only exposes this via the
        # signature.
        positional = sum(
            p.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            for p in inspect.signature(struct).parameters.values()
        )
        # msgspec leaves a Struct untracked by the cyclic GC when no field value can hold a
        # reference; the native builder does the same for rows of scalars, unless
        # `__post_init__` or a `__dict__` could add references it can't see.
        config = struct.__struct_config__
        untrackable = config.gc and not config.dict and not hasattr(struct, "__post_init__")
        spec = None
        if direct_error is None:
            spec = StructSpec(
                struct,
                fields,
                forbid_unknown_fields=info.forbid_unknown_fields,
                positional_fields=positional,
                untrackable=untrackable,
            )
        # Rows are encoded as maps/objects, which array_like Structs don't accept.
        map_error = None
        if info.array_like:
            map_error = f"{struct.__qualname__} is array_like; rows are encoded as maps"
        return Plan(
            spec=spec,
            direct_error=direct_error,
            fallback="msgpack",
            msgpack_one=msgspec.msgpack.Decoder(struct).decode,
            msgpack_many=msgspec.msgpack.Decoder(list[struct]).decode,
            msgpack_error=map_error,
            json_one=msgspec.json.Decoder(struct).decode,
            json_many=msgspec.json.Decoder(list[struct]).decode,
            json_error=map_error,
            json_non_finite_floats=False,
            # Its base class: malformed input becomes a fastpg error too.
            validation_error=msgspec.DecodeError,
            text_columns=tuple(f.encode_name for f in info.fields if cls._is_str(f.type)),
            field_columns=tuple(f.encode_name for f in info.fields),
        )

    @classmethod
    def plan_named_tuple(cls, target: type[tuple[Any, ...]]) -> Plan[tuple[Any, ...]]:
        """A `NamedTuple` (typing's or collections'): fields are classified as for Structs;
        instances are allocated and filled natively when that is what calling the class does
        (`_allocatable_named_tuple`), otherwise built by calling the class positionally with
        every field."""
        info = mi.type_info(target)
        if not isinstance(info, mi.NamedTupleType):  # guaranteed for namedtuple classes
            msg = f"msgspec does not describe {target!r} as a NamedTuple"
            raise TypeError(msg)
        annotations = typing.get_type_hints(target)
        attr_names = {f.name: f.name for f in info.fields}
        fields, direct_error = cls._field_args(target, info.fields, annotations, attr_names)
        spec = None
        if direct_error is None:
            spec = StructSpec(
                target,
                fields,
                forbid_unknown_fields=False,
                positional_fields=len(fields),
                named_tuple_defaults=[
                    (False, None) if f.required else (True, f.default) for f in info.fields
                ],
                named_tuple_alloc=cls._allocatable_named_tuple(target, len(fields)),
                untrackable=cls._untrackable_named_tuple(target),
            )
        route_error = (
            f"{target.__qualname__} is a NamedTuple, which is only built directly (the default"
            " strategy): rows are encoded as maps, and msgspec decodes NamedTuples from arrays"
        )
        return Plan(
            spec=spec,
            direct_error=direct_error,
            fallback="msgpack",
            msgpack_one=None,
            msgpack_many=None,
            msgpack_error=route_error,
            json_one=None,
            json_many=None,
            json_error=route_error,
            json_non_finite_floats=False,
            validation_error=msgspec.ValidationError,
            text_columns=(),
            field_columns=(),
        )


class Planner:
    """One cached `Plan` per `into=` type: dict, NamedTuples, msgspec Structs or pydantic
    models."""

    _plans: ClassVar[dict[type[object], Plan[Any]]] = {}
    _EXPECTED: ClassVar[str] = (
        "into= must be dict, a NamedTuple, or a msgspec.Struct or pydantic.BaseModel subclass"
    )

    @staticmethod
    def _dict_plan() -> Plan[dict[str, Any]]:
        """Rows as dicts keyed by column name, built directly."""
        route_error = (
            "dict rows are only built directly (the default strategy): through msgpack or"
            " JSON, values would lose their column types"
        )
        return Plan(
            spec=StructSpec.for_dict(),
            direct_error=None,
            fallback="msgpack",
            msgpack_one=None,
            msgpack_many=None,
            msgpack_error=route_error,
            json_one=None,
            json_many=None,
            json_error=route_error,
            json_non_finite_floats=False,
            validation_error=msgspec.ValidationError,
            text_columns=(),
            field_columns=(),
        )

    @staticmethod
    def _is_named_tuple(target: type[object]) -> TypeIs[type[tuple[Any, ...]]]:
        """Whether `target` is a namedtuple class (typing's `NamedTuple` or collections')."""
        return hasattr(target, "_fields") and issubclass(target, tuple)

    @classmethod
    def _build(cls, target: type[object]) -> Plan[Any]:
        if target is dict:
            return cls._dict_plan()
        if issubclass(target, msgspec.Struct):
            return StructPlanner.plan(target)
        if cls._is_named_tuple(target):
            return StructPlanner.plan_named_tuple(target)
        # Only consult pydantic if the application already imported it.
        if "pydantic" in sys.modules:
            from fastpg._pydantic import ModelPlanner

            if ModelPlanner.accepts(target):
                return ModelPlanner.plan(target)
        msg = f"{cls._EXPECTED}, got {target!r}"
        raise TypeError(msg)

    @classmethod
    def plan_for(cls, target: object) -> Plan[Any]:
        """The plan for `target`; raises TypeError for anything else (untyped callers)."""
        if not isinstance(target, type):
            msg = f"{cls._EXPECTED}, got {target!r}"
            raise TypeError(msg)
        known: type[object] = target
        plan = cls._plans.get(known)  # absent: first use of this type
        if plan is None:
            plan = cls._plans[known] = cls._build(known)
        return plan
