import logging

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import PlainTextResponse

from blitzecdn.api.dependencies import ControlPlaneDependency, require_operator
from blitzecdn.api.v1_operations import AuditEvent, as_v1

router = APIRouter()
_LOGGER = logging.getLogger(__name__)
_METRICS_WINDOW = 100


@router.get("/health")
def health(response: Response, control: ControlPlaneDependency) -> dict[str, str]:
    """Whether this controller can actually answer, not merely respond."""
    try:
        control.workflow_history.list_workflows(1)
        if not control.broker_ready():
            raise ConnectionError("Redis did not answer PING")
    except Exception as exc:
        _LOGGER.exception("health check failed")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable", "detail": type(exc).__name__}
    return {"status": "ok"}


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    dependencies=[Depends(require_operator)],
)
def metrics(control: ControlPlaneDependency) -> str:
    """Prometheus gauges derived from persisted control-plane state."""
    recent = control.deployments.list_deployments(_METRICS_WINDOW)
    expiring = control.certificates.expiring_certificates()
    unrenewable = len([status_ for status_ in expiring if not status_.renewable])
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
        f"blitzecdn_sites {len(control.dns.list_sites())}",
        "# HELP blitzecdn_certificates_expiring Managed certificates near expiry.",
        "# TYPE blitzecdn_certificates_expiring gauge",
        f"blitzecdn_certificates_expiring {len(expiring)}",
        "# HELP blitzecdn_certificates_unrenewable Expiring but not reissuable.",
        "# TYPE blitzecdn_certificates_unrenewable gauge",
        f"blitzecdn_certificates_unrenewable {unrenewable}",
        "# HELP blitzecdn_deployments Deployments in the recent window, by status.",
        "# TYPE blitzecdn_deployments gauge",
    ]
    lines.extend(
        f'blitzecdn_deployments{{status="{state}"}} {by_status[state]}'
        for state in sorted(by_status)
    )
    return "\n".join(lines) + "\n"


@router.get(
    "/v1/audit-events",
    response_model=list[AuditEvent],
    dependencies=[Depends(require_operator)],
)
def audit_events(
    control: ControlPlaneDependency,
    limit: int = Query(100, ge=1, le=500),
) -> list[AuditEvent]:
    return [as_v1(item, AuditEvent) for item in control.audit.list_audit_events(limit)]
