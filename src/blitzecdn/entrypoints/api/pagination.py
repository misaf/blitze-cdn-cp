"""The pagination contract shared by every collection endpoint."""

from collections.abc import Callable, Sequence
from typing import Annotated, cast

from fastapi import Depends, Query
from fastapi_pagination import LimitOffsetPage, LimitOffsetParams
from fastapi_pagination import paginate as paginate_sequence
from fastapi_pagination.api import create_page
from pydantic import ConfigDict

from blitzecdn.core.domain.pagination import PageResult


class Params(LimitOffsetParams):
    # The library builds its dependency from these FieldInfo objects directly.
    limit: int = Query(100, ge=1, le=500)
    offset: int = Query(0, ge=0, le=2**63 - 1)


class Page[T](LimitOffsetPage[T]):
    model_config = ConfigDict(json_schema_mode_override="validation")
    __params_type__ = Params


PaginationParams = Annotated[Params, Depends()]


def paginate[Item, Output](
    items: Sequence[Item], convert: Callable[[Item], Output]
) -> Page[Output]:
    # These collections are already materialized configuration or file data.
    # `safe=True` suppresses the library's ORM warning for this intentional use.
    return cast(
        Page[Output],
        paginate_sequence(
            items, transformer=lambda page: [convert(item) for item in page], safe=True
        ),
    )


def stored_page[Item, Output](
    result: PageResult[Item], params: Params, convert: Callable[[Item], Output]
) -> Page[Output]:
    return cast(
        Page[Output],
        create_page(
            [convert(item) for item in result.items], total=result.total, params=params
        ),
    )
