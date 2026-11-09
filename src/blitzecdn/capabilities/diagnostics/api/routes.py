from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends

from blitzecdn.capabilities.diagnostics.domain.status import OperationalStatus
from blitzecdn.capabilities.diagnostics.service.status import DiagnosticsService
from blitzecdn.core.ports.operations import AuditTrail
from blitzecdn.entrypoints.api.dependencies import require_operator
from blitzecdn.entrypoints.api.models import AuditEvent, as_operation
from blitzecdn.entrypoints.api.pagination import Page, PaginationParams, stored_page

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.get("/v3/status", response_model=OperationalStatus)
def operational_status(
    diagnostics: FromDishka[DiagnosticsService],
) -> OperationalStatus:
    """Local queue, worker, workflow and certificate observations."""
    return diagnostics.status()


@router.get("/v3/audit-events", response_model=Page[AuditEvent])
def audit_events(
    audit: FromDishka[AuditTrail],
    params: PaginationParams,
) -> Page[AuditEvent]:
    return stored_page(
        audit.paginate_audit_events(limit=params.limit, offset=params.offset),
        params,
        lambda item: as_operation(item, AuditEvent),
    )
