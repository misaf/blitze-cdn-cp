"""SQL pagination with domain values returned before the session closes."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi_pagination import LimitOffsetPage
from fastapi_pagination.bases import AbstractParams, RawParams
from fastapi_pagination.config import Config
from fastapi_pagination.ext.sqlalchemy import paginate
from sqlalchemy import Select
from sqlalchemy.orm import Session

from blitzecdn.core.domain.pagination import PageResult


@dataclass
class _Params(AbstractParams):
    limit: int
    offset: int

    def to_raw_params(self) -> RawParams:
        return RawParams(limit=self.limit, offset=self.offset)


def read_page[Row, Item](
    session: Session,
    query: Select[tuple[Row]],
    convert: Callable[[Row], Item],
    *,
    limit: int,
    offset: int,
) -> PageResult[Item]:
    # Select the adapter's page explicitly: an HTTP request may have a different
    # page model in context. No ORM row escapes the session or reaches a route.
    page = paginate(
        session,
        query,
        params=_Params(limit, offset),
        config=Config(page_cls=LimitOffsetPage[Any]),
    )
    return PageResult(items=[convert(row) for row in page.items], total=page.total)
