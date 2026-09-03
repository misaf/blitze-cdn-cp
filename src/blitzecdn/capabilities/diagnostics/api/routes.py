from fastapi import APIRouter, Depends, Query

from blitzecdn.api.dependencies import ControlPlaneDependency, require_operator
from blitzecdn.api.operations import AuditEvent, as_operation

router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/v1/audit-events", response_model=list[AuditEvent])
def audit_events(
    control: ControlPlaneDependency,
    limit: int = Query(100, ge=1, le=500),
) -> list[AuditEvent]:
    return [
        as_operation(item, AuditEvent)
        for item in control.audit.list_audit_events(limit)
    ]
