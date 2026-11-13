"""Reading compiled releases over HTTP.

Read-only by design: a different release comes from changing a zone, rule or
record. ``GET /v3/releases/current`` compiles without recording, so polling it
leaves no history.
"""

from __future__ import annotations

from typing import Annotated, Any

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Query

from blitzecdn.capabilities.releases.api.models import (
    Artifact,
    Release,
    ReleaseDetail,
    ReleaseDiff,
)
from blitzecdn.capabilities.releases.service import ReleaseService
from blitzecdn.delivery.api.dependencies import require_operator
from blitzecdn.delivery.api.errors import (
    NOT_FOUND_RESPONSES,
    OPERATOR_RESPONSES,
)
from blitzecdn.delivery.api.models import as_operation
from blitzecdn.delivery.api.pagination import Page, PaginationParams, stored_page

router = APIRouter(
    tags=["releases"],
    route_class=DishkaSyncRoute,
    dependencies=[Depends(require_operator)],
    responses=OPERATOR_RESPONSES,
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


@router.get("/v3/releases", response_model=Page[Release], operation_id="releases_list")
def releases(
    releases: FromDishka[ReleaseService],
    params: PaginationParams,
) -> Page[Release]:
    return stored_page(
        releases.paginate_releases(limit=params.limit, offset=params.offset),
        params,
        lambda item: as_operation(_summary(item), Release),
    )


@router.get(
    "/v3/releases/current",
    response_model=ReleaseDetail,
    operation_id="releases_current",
)
def current_release(
    releases: FromDishka[ReleaseService],
    host_limit: Annotated[
        str | None,
        Query(
            max_length=512,
            description=(
                "Edges to validate against, as on POST /v3/deployments; "
                "omitted means every registered edge. A selector, not a page."
            ),
        ),
    ] = None,
) -> ReleaseDetail:
    """What canonical state compiles to right now, without recording it.

    ``host_limit`` validates against just those edges, such as a canary.
    """
    return as_operation(
        _summary(releases.compile(host_limit=host_limit), detail=True), ReleaseDetail
    )


@router.get(
    "/v3/releases/current/diff",
    response_model=ReleaseDiff,
    operation_id="releases_current_diff",
)
def current_release_diff(releases: FromDishka[ReleaseService]) -> ReleaseDiff:
    """What deploying now would change on the edges, as a unified diff per file.

    The rendered tree current state compiles to, against the one the fleet is
    running. Records nothing; secrets are never in a bundle, so never in a diff.
    """
    return as_operation(releases.diff().summary(), ReleaseDiff)


@router.get(
    "/v3/releases/{release_id}",
    response_model=ReleaseDetail,
    responses=NOT_FOUND_RESPONSES,
    operation_id="releases_get",
)
def release(release_id: str, releases: FromDishka[ReleaseService]) -> ReleaseDetail:
    """One recorded release, with the explanation of every host in it."""
    return as_operation(_summary(releases.get(release_id), detail=True), ReleaseDetail)


@router.get(
    "/v3/releases/{release_id}/artifacts/{artifact}",
    response_model=Artifact,
    responses=NOT_FOUND_RESPONSES,
    operation_id="releases_artifact",
)
def artifact(
    release_id: str, artifact: str, releases: FromDishka[ReleaseService]
) -> Artifact:
    """One artifact document, exactly as the edges were handed it."""
    document = releases.get(release_id).artifact(artifact)
    return Artifact(
        name=document.name, digest=document.digest, document=document.document
    )
