import logging

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import PlainTextResponse

from blitzecdn.entrypoints.api.dependencies import ServicesDependency, require_operator

router = APIRouter()
_LOGGER = logging.getLogger(__name__)
_METRICS_WINDOW = 100


@router.get("/health")
def health(response: Response, control: ServicesDependency) -> dict[str, str]:
    """Whether this controller can actually answer, not merely respond.

    Every check is a feature contribution; the first failure decides, and is named
    in the body.
    """
    for check in control.health_checks:
        try:
            check.check()
        except Exception as exc:
            _LOGGER.exception("health check %s failed", check.name)
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {
                "status": "unavailable",
                "check": check.name,
                "detail": type(exc).__name__,
            }
    return {"status": "ok"}


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    dependencies=[Depends(require_operator)],
)
def metrics(control: ServicesDependency) -> str:
    """Prometheus gauges derived from persisted control-plane state."""
    recent = control.deployments.list_deployments(_METRICS_WINDOW)
    by_status: dict[str, int] = {}
    for deployment in recent:
        by_status[deployment.status.value] = (
            by_status.get(deployment.status.value, 0) + 1
        )
    lines = [
        "# HELP blitzecdn_edges Registered edge servers.",
        "# TYPE blitzecdn_edges gauge",
        f"blitzecdn_edges {len(control.edges.list_edges())}",
        "# HELP blitzecdn_sites Derived virtual hosts.",
        "# TYPE blitzecdn_sites gauge",
        f"blitzecdn_sites {len(control.sites.list_sites())}",
        "# HELP blitzecdn_deployments Deployments in the recent window, by status.",
        "# TYPE blitzecdn_deployments gauge",
    ]
    lines.extend(
        f'blitzecdn_deployments{{status="{state}"}} {by_status[state]}'
        for state in sorted(by_status)
    )
    return "\n".join(lines) + "\n"
