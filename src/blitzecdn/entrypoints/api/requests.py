"""HTTP request bodies, omittable PATCH values, and shared fleet selection."""

from __future__ import annotations

from typing import Annotated, Any, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from blitzecdn.core.domain.validation import EDGE_LIMIT

T = TypeVar("T")


def _reject_null(value: Any) -> Any:
    if value is None:
        raise ValueError("null is not allowed; omit the field to keep its value")
    return value


def _omit_default(schema: dict[str, Any]) -> None:
    schema.pop("default", None)


# None represents an omitted field internally, never an accepted JSON value.
PatchValue = Annotated[
    T | SkipJsonSchema[None],
    BeforeValidator(_reject_null),
    Field(json_schema_extra=_omit_default),
]


class RequestModel(BaseModel):
    """HTTP request bodies reject fields the current API does not define."""

    model_config = ConfigDict(extra="forbid")


class FleetRequest(RequestModel):
    host_limit: str | None = Field(
        default=None,
        max_length=512,
        pattern=EDGE_LIMIT.pattern,
        description=(
            "Omit or send null to target all registered edges; "
            "otherwise limit by edge name or pattern."
        ),
    )


__all__ = ["FleetRequest", "PatchValue", "RequestModel"]
