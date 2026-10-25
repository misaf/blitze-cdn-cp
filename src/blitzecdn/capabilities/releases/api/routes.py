"""Reading compiled releases over HTTP.

Read-only by design: a different release comes from changing a zone, rule or
record. ``GET /v1/releases/current`` compiles without recording, so polling it
leaves no history.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status

from blitzecdn.capabilities.releases.api.models import Release, ReleaseDetail
from blitzecdn.entrypoints.api.dependencies import ServicesDependency, require_operator
from blitzecdn.entrypoints.api.models import as_operation

router = APIRouter(dependencies=[Depends(require_operator)])


def _summary(release: Any) -> dict[str, Any]:
    """The published shape of a release, with its computed digest and servability."""
    return {
        **release.model_dump(mode="json"),
        "id": release.id,
        "digest": release.digest,
        "servable": release.servable,
        "artifacts": [
            {"name": artifact.name, "digest": artifact.digest}
            for artifact in release.artifacts
        ],
    }


@router.get("/v1/releases", response_model=list[Release])
def releases(
    control: ServicesDependency,
    limit: int = Query(20, ge=1, le=100),
) -> list[Release]:
    """Recorded releases, newest first."""
    return [
        as_operation(_summary(item), Release)
        for item in control.releases.list_releases(limit)
    ]


@router.get("/v1/releases/current", response_model=ReleaseDetail)
def current_release(
    control: ServicesDependency,
    host_limit: str | None = Query(default=None, max_length=512),
) -> ReleaseDetail:
    """What canonical state compiles to right now, without recording it.

    ``host_limit`` validates against just those edges, such as a canary.
    """
    return as_operation(
        _summary(control.releases.compile(host_limit=host_limit)), ReleaseDetail
    )


@router.get("/v1/releases/{release_id}", response_model=ReleaseDetail)
def release(release_id: str, control: ServicesDependency) -> ReleaseDetail:
    """One recorded release, with the explanation of every host in it."""
    return as_operation(_summary(control.releases.get(release_id)), ReleaseDetail)


@router.get(
    "/v1/releases/{release_id}/artifacts/{name}",
    status_code=status.HTTP_200_OK,
)
def artifact(release_id: str, name: str, control: ServicesDependency) -> dict[str, Any]:
    """One artifact document, exactly as the edges were handed it; unmodelled."""
    document = control.releases.get(release_id).artifact(name)
    return {
        "name": document.name,
        "digest": document.digest,
        "document": document.document,
    }
