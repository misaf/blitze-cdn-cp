"""Models whose fields exist only once the feature registry has composed them.

A site policy, and every representation of it, has one field per contribution.
The classes that carry such a value are declared at import time, as are routes
and the models that nest them, but the contributions are known only when the
registry loads. So the named class is static and its fields live on a subclass
generated later.

Using the static class anywhere a type is expected (a field, a request body, a
response model, ``Page[...]``) validates and serializes as the generated model,
looked up when a value arrives rather than when the type was declared. The
JSON schema names the generated model, so it is published once and referenced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Self, cast

from pydantic import BaseModel, GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema

__all__ = ["ComposedModel"]


def _serialize(value: BaseModel, info: core_schema.SerializationInfo) -> Any:
    return value.model_dump(
        mode=info.mode,
        by_alias=info.by_alias,
        exclude_unset=info.exclude_unset,
        exclude_defaults=info.exclude_defaults,
        exclude_none=info.exclude_none,
    )


@dataclass(frozen=True, slots=True)
class _Lazy:
    """The static class's schema: look the generated model up per value."""

    static: type[ComposedModel]

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        static = self.static

        def validate(value: Any) -> ComposedModel:
            model = static.composed()
            return value if isinstance(value, model) else model.model_validate(value)

        return core_schema.no_info_plain_validator_function(
            validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                _serialize, info_arg=True
            ),
        )

    def __get_pydantic_json_schema__(
        self, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        model = self.static.composed()
        inner = dict(model.__pydantic_core_schema__)
        ref = inner.setdefault("ref", f"{model.__module__}.{model.__qualname__}")
        return handler(
            core_schema.definitions_schema(
                core_schema.definition_reference_schema(ref),
                [inner],
            )
        )


class ComposedModel(BaseModel):
    """A static model whose fields come from what it is composed from.

    A subclass names the source its fields derive from and builds the model
    from it; the generated model is cached until the source changes.
    Constructing or validating the static class builds the generated one.
    """

    #: Set on generated models only.
    __composed__: ClassVar[bool] = False

    # A model-level JSON function always inlines the schema it produces, which
    # would publish the generated model again at every use. Generated models get
    # pydantic's default back in `compose_fields`.
    __get_pydantic_json_schema__ = None  # type: ignore[assignment]

    @classmethod
    def composition_source(cls) -> object:
        """What the fields derive from; a new value composes the model again."""
        raise NotImplementedError

    @classmethod
    def compose(cls, source: Any) -> type[Self]:
        """Build the model for ``source``, usually through :meth:`compose_fields`."""
        raise NotImplementedError

    @classmethod
    def composed(cls) -> type[Self]:
        """The generated model for the current source."""
        static = cls._static()
        source = static.composition_source()
        # Keyed by identity and kept, so a source composed again (a test that
        # restores the real registry) gets back the model its values belong to.
        cache: dict[int, tuple[object, type[ComposedModel]]]
        cache = static.__dict__.get("__composed_cache__") or {}
        if id(source) not in cache:
            cache = {**cache, id(source): (source, static.compose(source))}
            type.__setattr__(static, "__composed_cache__", cache)
        return cast(type[Self], cache[id(source)][1])

    @classmethod
    def compose_fields(cls, fields: dict[str, tuple[Any, Any]]) -> type[Self]:
        """A generated subclass of this static class carrying ``fields``.

        Each field is ``(annotation, default)``, as for ``create_model``. Built
        through the metaclass so ``__composed__`` holds while pydantic builds it.
        """
        namespace: dict[str, Any] = {
            "__module__": cls.__module__,
            "__qualname__": cls.__qualname__,
            "__doc__": cls.__doc__,
            "__composed__": True,
            "__get_pydantic_json_schema__": BaseModel.__dict__[
                "__get_pydantic_json_schema__"
            ],
            "__annotations__": {name: kind for name, (kind, _) in fields.items()},
        }
        namespace.update({name: default for name, (_, default) in fields.items()})
        metaclass: Any = type(cls)
        return cast(type[Self], metaclass(cls.__name__, (cls,), namespace))

    @classmethod
    def _static(cls) -> type[ComposedModel]:
        for klass in cls.__mro__:
            if issubclass(klass, ComposedModel) and not klass.__dict__.get(
                "__composed__", False
            ):
                return klass
        raise TypeError(f"{cls.__name__} has no static class")  # pragma: no cover

    @classmethod
    def _target(cls) -> type[Self]:
        return cls if cls.__composed__ else cls.composed()

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        return object.__new__(cls._target())

    @classmethod
    def model_validate(cls, obj: Any, **options: Any) -> Self:
        return super(ComposedModel, cls._target()).model_validate(obj, **options)

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, **options: Any
    ) -> Self:
        return super(ComposedModel, cls._target()).model_validate_json(
            json_data, **options
        )

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> Self:
        return super(ComposedModel, cls._target()).model_construct(
            _fields_set, **values
        )

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        if cls.__composed__:
            return handler(source)
        return handler(Annotated[source, _Lazy(cls)])
