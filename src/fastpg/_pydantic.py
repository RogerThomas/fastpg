"""Pydantic v2 support (optional dependency).

pydantic-core has no Rust API: it isn't published as a crate, and a second compiled copy
couldn't share type objects or schemas with the user's installed pydantic. Its native
entry points are Python-level: `SchemaValidator.validate_python` (Python objects),
`validate_json` (bytes parsed by jiter, in Rust) and `validate_strings`. fastpg offers two
routes:

* **json** (validated): Rust encodes rows to JSON; `TypeAdapter(...).validate_json` parses and
  validates them in pydantic-core. No intermediate Python objects; full pydantic semantics.
* **direct** (unvalidated, type-checked): Rust builds each instance like
  `BaseModel.model_construct`: a `__dict__` of decoded values plus the three
  `__pydantic_*__` slots, with no `__init__` and no validation. This is only offered when the
  model's core schema shows validation would be a no-op for values of the declared types:
  plain scalar fields, no validators or constraints, no `extra="allow"`, no private
  attributes, no post-init, no custom `__init__`, simple aliases and immutable defaults.
  Column types are checked against field types once per statement, exactly like msgspec
  direct mode.
"""

import types
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, TypeIs, get_args, get_origin

from pydantic import AliasChoices, AliasPath, BaseModel, TypeAdapter, ValidationError
from pydantic_core import PydanticUndefined

from fastpg._native import StructSpec
from fastpg._spec import FieldArg, Kind, NeedsValidationError, Plan

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo


class ModelPlanner:
    """Builds `Plan`s for pydantic v2 models (see the module docstring)."""

    _SCALARS: ClassVar[dict[str, Kind]] = {
        "any": Kind.ANY,
        "bool": Kind.BOOL,
        "int": Kind.INT,
        "float": Kind.FLOAT,
        "str": Kind.STR,
        "bytes": Kind.BYTES,
        "datetime": Kind.DATETIME,
        "date": Kind.DATE,
        "time": Kind.TIME,
        "uuid": Kind.UUID,
        "decimal": Kind.DECIMAL,
    }
    # Validated by a per-field TypeAdapter(...).validate_json from json/jsonb columns.
    _JSON_SHAPED: ClassVar[frozenset[str]] = frozenset(
        {"list", "dict", "tuple", "set", "frozenset", "model", "typed-dict", "dataclass"},
    )
    # Schema keys that don't change validation of an already-correctly-typed value.
    _INERT_KEYS: ClassVar[frozenset[str]] = frozenset({"type", "metadata", "ref"})
    # Defaults that are safe to share between instances (pydantic copies mutable ones).
    _IMMUTABLE_DEFAULTS: ClassVar[tuple[type, ...]] = (type(None), bool, int, float, str, bytes)

    @classmethod
    def _classify(cls, schema: dict[str, Any]) -> tuple[Kind, bool]:
        nullable = False
        if schema["type"] == "nullable":
            nullable = True
            schema = schema["schema"]
        kind = cls._SCALARS.get(schema["type"])  # absent: not a scalar
        if kind is not None:
            extra = {k: v for k, v in schema.items() if k not in cls._INERT_KEYS}
            if extra.pop("microseconds_precision", "truncate") != "truncate" or extra:
                msg = f"schema options {extra} need pydantic validation"
                raise NeedsValidationError(msg)
            # `Any` takes None, as pydantic validation does: NULL is no error there.
            return kind, nullable or kind is Kind.ANY
        if schema["type"] in cls._JSON_SHAPED:
            return Kind.JSON, nullable
        msg = f"{schema['type']!r} fields need pydantic validation"
        raise NeedsValidationError(msg)

    @staticmethod
    def _check_model(model: type[BaseModel]) -> dict[str, Any]:
        """The model-fields schema, if construction is equivalent to validation."""
        schema: dict[str, Any] = dict(model.__pydantic_core_schema__)
        if schema["type"] != "model" or schema["schema"]["type"] != "model-fields":
            msg = f"core schema type {schema['type']!r} needs pydantic validation"
            raise NeedsValidationError(msg)
        # Optional schema flags.
        if schema.get("custom_init") or schema.get("root_model") or schema.get("post_init"):
            msg = "custom __init__ / RootModel / model_post_init need pydantic validation"
            raise NeedsValidationError(msg)
        decorators = model.__pydantic_decorators__
        if (
            decorators.validators
            or decorators.field_validators
            or decorators.root_validators
            or decorators.model_validators
        ):
            msg = "validators need pydantic validation"
            raise NeedsValidationError(msg)
        if model.model_config.get("extra") == "allow" or model.__private_attributes__:
            msg = "extra='allow' / private attributes need pydantic validation"
            raise NeedsValidationError(msg)
        return schema["schema"]

    @staticmethod
    def _is_str(annotation: object) -> bool:
        """Whether a field annotation is `str` or `str | None` (constraints don't matter)."""
        if isinstance(annotation, types.UnionType):
            members = [m for m in annotation.__args__ if m is not types.NoneType]
            if len(members) != 1:
                return False
            annotation = members[0]
        if get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        return annotation is str

    @staticmethod
    def _alias(name: str, info: FieldInfo) -> str:
        alias = info.validation_alias if info.validation_alias is not None else info.alias
        if isinstance(alias, AliasPath | AliasChoices):
            msg = f"{name}: AliasPath/AliasChoices need pydantic validation"
            raise NeedsValidationError(msg)
        return alias or name

    @classmethod
    def _default(cls, name: str, info: FieldInfo) -> tuple[bool, object]:
        if info.default_factory is not None or info.validate_default:
            msg = f"{name}: default_factory / validate_default need pydantic validation"
            raise NeedsValidationError(msg)
        if info.default is PydanticUndefined:
            return False, None
        if not isinstance(info.default, cls._IMMUTABLE_DEFAULTS):
            msg = f"{name}: mutable default needs pydantic validation (pydantic copies it)"
            raise NeedsValidationError(msg)
        return True, info.default

    @classmethod
    def _direct_spec(cls, model: type[BaseModel]) -> StructSpec:
        model_fields = cls._check_model(model)
        fields: list[FieldArg] = []
        defaults: list[tuple[bool, object]] = []
        for name, info in model.model_fields.items():
            schema: dict[str, Any] = model_fields["fields"][name]["schema"]
            if schema["type"] == "default":
                schema = schema["schema"]
            kind, nullable = cls._classify(schema)
            decoder = None
            if kind is Kind.JSON:
                annotation: Any = info.annotation
                if info.metadata:
                    annotation = Annotated[annotation, *info.metadata]
                decoder = TypeAdapter(annotation).validate_json
            defaults.append(cls._default(name, info))
            alias = cls._alias(name, info)
            fields.append((name, alias, kind, nullable, info.is_required(), decoder))
        return StructSpec(
            model,
            fields,
            forbid_unknown_fields=model.model_config.get("extra") == "forbid",
            positional_fields=0,
            pydantic_defaults=defaults,
        )

    @staticmethod
    def accepts(target: type[object]) -> TypeIs[type[BaseModel]]:
        """Whether `target` is a pydantic model class."""
        return issubclass(target, BaseModel)

    @classmethod
    def write_alias(cls, name: str, info: FieldInfo) -> str | None:
        """Column a field is written to by `insert_many`/`copy_in`: the same name `select_*`
        matches it by, so models round-trip. None for `AliasPath`/`AliasChoices` aliases."""
        try:
            return cls._alias(name, info)
        except NeedsValidationError:
            return None

    @classmethod
    def plan(cls, model: type[BaseModel]) -> Plan[BaseModel]:
        try:
            spec, direct_error = cls._direct_spec(model), None
        except NeedsValidationError as e:
            spec, direct_error = None, f"{model.__qualname__}: {e}"
        list_of: Any = list[model]  # a runtime type object, not a type expression
        text_columns = tuple(
            alias
            for name, info in model.model_fields.items()
            if cls._is_str(info.annotation) and (alias := cls.write_alias(name, info)) is not None
        )
        return Plan(
            spec=spec,
            direct_error=direct_error,
            fallback="json",
            msgpack_one=None,
            msgpack_many=None,
            msgpack_error="pydantic models are decoded from JSON (strategy='json') or directly",
            json_one=TypeAdapter(model).validate_json,
            json_many=TypeAdapter[list[BaseModel]](list_of).validate_json,
            json_error=None,
            validation_error=ValidationError,
            text_columns=text_columns,
        )
