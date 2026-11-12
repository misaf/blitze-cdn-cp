import logging
from typing import Literal

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST

from blitzecdn.capabilities.deployments import DeploymentService
from blitzecdn.capabilities.diagnostics.api.metrics import (
    exposition,
    gauge,
    labelled_gauge,
    operational_metrics,
)
from blitzecdn.capabilities.diagnostics.service.status import DiagnosticsService
from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.sites import SiteReader
from blitzecdn.core.features import Operations
from blitzecdn.delivery.api.dependencies import require_operator
from blitzecdn.delivery.api.errors import GLOBAL_RESPONSES, OPERATOR_RESPONSES
from blitzecdn.delivery.api.models import Model
from blitzecdn.delivery.api.validation import VALIDATION_RESPONSE

router = APIRouter(route_class=DishkaSyncRoute)
_LOGGER = logging.getLogger(__name__)
_METRICS_WINDOW = 100


class HealthUnavailable(Model):
    """A failed health check: not an error message, so not `ErrorResponse`."""

    status: Literal["unavailable"]
    #: The name of the first check that failed.
    check: str
    #: The exception's type, never its message, which can carry credentials.
    detail: str


@router.get(
    "/health",
    responses={
        503: {
            "model": HealthUnavailable,
            "description": "A health check failed; `check` names the first.",
        }
    },
)
def health(response: Response, operations: FromDishka[Operations]) -> dict[str, str]:
    """Whether this controller can actually answer, not merely respond.

    Every check is a feature contribution; the first failure decides, and is named
    in the body.
    """
    for check in operations.health_checks:
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
    # Restated without a model, so a text route documents its errors as JSON
    # rather than as `text/plain`.
    responses={422: VALIDATION_RESPONSE, **GLOBAL_RESPONSES, **OPERATOR_RESPONSES},
    dependencies=[Depends(require_operator)],
)
def metrics(
    deployments: FromDishka[DeploymentService],
    edges: FromDishka[EdgeOperationsService],
    sites: FromDishka[SiteReader],
    diagnostics: FromDishka[DiagnosticsService],
) -> PlainTextResponse:
    """Prometheus gauges derived from persisted control-plane state."""
    recent = deployments.list_deployments(_METRICS_WINDOW)
    by_status: dict[str, int] = {}
    for deployment in recent:
        by_status[deployment.status.value] = (
            by_status.get(deployment.status.value, 0) + 1
        )
    families = [
        gauge("edges", "Registered edge servers.", len(edges.list_edges())),
        gauge("sites", "Derived virtual hosts.", len(sites.list_sites())),
        labelled_gauge(
            "deployments",
            "Deployments in the recent window, by status.",
            "status",
            ((state, by_status[state]) for state in sorted(by_status)),
        ),
        *operational_metrics(diagnostics.status()),
    ]
    return PlainTextResponse(exposition(families), media_type=CONTENT_TYPE_LATEST)
