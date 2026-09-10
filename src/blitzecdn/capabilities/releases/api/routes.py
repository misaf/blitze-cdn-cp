"""Reading compiled releases over HTTP.

Read-only, and that is a design statement rather than an omission. A release is
compiled from canonical state; the way to get a different one is to change a
zone, a rule or a record. An endpoint that created a release from a body would
be a second source of desired state, which is the thing the whole capability
exists to prevent.

``POST /v1/releases`` is therefore absent and ``GET /v1/releases/current``
compiles without recording: asking what current state means must not be an act
that leaves a trace, or every dashboard poll becomes a row in the history of
what the fleet was asked to serve.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status

from blitzecdn.api.dependencies import ControlPlaneDependency, require_operator
from blitzecdn.api.models import as_operation
from blitzecdn.capabilities.releases.api.models import Release, ReleaseDetail

router = APIRouter(dependencies=[Depends(require_operator)])


def _summary(release: Any) -> dict[str, Any]:
    """The published shape of a release, digests included.

    ``digest`` and ``servable`` are computed properties on the domain model
    rather than fields, so they do not appear in ``model_dump`` and are added
    here. That is the point at which the wire shape and the model are visibly
    two things, which is what the ``api`` package is for.
    """
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
    control: ControlPlaneDependency,
    limit: int = Query(20, ge=1, le=100),
) -> list[Release]:
    """Recorded releases, newest first."""
    return [
        as_operation(_summary(item), Release)
        for item in control.releases.list_releases(limit)
    ]


@router.get("/v1/releases/current", response_model=ReleaseDetail)
def current_release(
    control: ControlPlaneDependency,
    host_limit: str | None = Query(default=None, max_length=512),
) -> ReleaseDetail:
    """What canonical state compiles to right now, without recording it.

    ``host_limit`` narrows the edges the compilation is validated against, so a
    client can ask "would this converge on the canary" before starting a run
    that would find out the expensive way.
    """
    return as_operation(
        _summary(control.releases.compile(host_limit=host_limit)), ReleaseDetail
    )


@router.get("/v1/releases/{release_id}", response_model=ReleaseDetail)
def release(release_id: str, control: ControlPlaneDependency) -> ReleaseDetail:
    """One recorded release, with the explanation of every host in it."""
    return as_operation(_summary(control.releases.get(release_id)), ReleaseDetail)


@router.get(
    "/v1/releases/{release_id}/artifacts/{name}",
    status_code=status.HTTP_200_OK,
)
def artifact(
    release_id: str, name: str, control: ControlPlaneDependency
) -> dict[str, Any]:
    """One artifact document, exactly as the edges were handed it.

    Unmodelled on purpose: the document is a mapping of Ansible variables whose
    keys come from whichever capabilities are installed, so a schema for it
    would be this repository claiming to know what a wheel it has never heard
    of contributes.
    """
    document = control.releases.get(release_id).artifact(name)
    return {
        "name": document.name,
        "digest": document.digest,
        "document": document.document,
    }
