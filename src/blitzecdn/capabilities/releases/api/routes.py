"""Reading compiled releases over HTTP.

Read-only by design: a different release comes from changing a zone, rule or
record. ``GET /v3/releases/current`` compiles without recording, so polling it
leaves no history.
"""

from __future__ import annotations

from typing import Annotated, Any

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Query, status

from blitzecdn.capabilities.releases.api.models import Release, ReleaseDetail
from blitzecdn.capabilities.releases.service import ReleaseService
from blitzecdn.entrypoints.api.dependencies import require_operator
from blitzecdn.entrypoints.api.models import as_operation
from blitzecdn.entrypoints.api.pagination import Page, PaginationParams, stored_page

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


def _summary(release: Any, *, detail: bool = False) -> dict[str, Any]:
    """The published shape of a release, with its computed digest and servability."""
    excluded = {"targets", "sites"}
    if not detail:
        excluded.add("explanations")
    return {
        **release.model_dump(mode="json", exclude=excluded),
        "id": release.id,
        "digest": release.digest,
        "servable": release.servable,
        "artifacts": [
            {"name": artifact.name, "digest": artifact.digest}
            for artifact in release.artifacts
        ],
    }


@router.get("/v3/releases", response_model=Page[Release])
def releases(
    releases: FromDishka[ReleaseService],
    params: PaginationParams,
) -> Page[Release]:
    return stored_page(
        releases.paginate_releases(limit=params.limit, offset=params.offset),
        params,
        lambda item: as_operation(_summary(item), Release),
    )


@router.get("/v3/releases/current", response_model=ReleaseDetail)
def current_release(
    releases: FromDishka[ReleaseService],
    host_limit: Annotated[str | None, Query(max_length=512)] = None,
) -> ReleaseDetail:
    """What canonical state compiles to right now, without recording it.

    ``host_limit`` validates against just those edges, such as a canary.
    """
    return as_operation(
        _summary(releases.compile(host_limit=host_limit), detail=True), ReleaseDetail
    )


@router.get("/v3/releases/{release_id}", response_model=ReleaseDetail)
def release(release_id: str, releases: FromDishka[ReleaseService]) -> ReleaseDetail:
    """One recorded release, with the explanation of every host in it."""
    return as_operation(_summary(releases.get(release_id), detail=True), ReleaseDetail)


@router.get(
    "/v3/releases/{release_id}/artifacts/{name}",
    status_code=status.HTTP_200_OK,
)
def artifact(
    release_id: str, name: str, releases: FromDishka[ReleaseService]
) -> dict[str, Any]:
    """One artifact document, exactly as the edges were handed it; unmodelled."""
    document = releases.get(release_id).artifact(name)
    return {
        "name": document.name,
        "digest": document.digest,
        "document": document.document,
    }
