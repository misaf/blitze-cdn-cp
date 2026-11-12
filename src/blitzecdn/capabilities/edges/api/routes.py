from typing import Annotated

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Query, Response, status

from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.edges.api.models import Edge, EdgePatch
from blitzecdn.capabilities.edges.service.operations import (
    EdgeRemovalJob,
    remove_dedupe_key,
)
from blitzecdn.capabilities.jobs import Job, OperationService
from blitzecdn.core.ports.idempotency import IdempotencyKeyStore
from blitzecdn.delivery.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.delivery.api.errors import (
    BODY_RESPONSES,
    CONFLICT_RESPONSES,
    NOT_FOUND_RESPONSES,
    OPERATOR_RESPONSES,
)
from blitzecdn.delivery.api.idempotency import (
    IdempotencyKeyDependency,
    idempotent,
)
from blitzecdn.delivery.api.models import Operation, as_queued
from blitzecdn.delivery.api.pagination import Page, paginate
from blitzecdn.delivery.api.responses import (
    ACCEPTED_RESPONSE,
    polling_headers,
)

router = APIRouter(
    route_class=DishkaSyncRoute,
    dependencies=[Depends(require_operator)],
    responses=OPERATOR_RESPONSES,
)


@router.get("/v3/edges", response_model=Page[Edge])
def list_edges(edges: FromDishka[EdgeOperationsService]) -> Page[Edge]:
    """Every registered edge, which is exactly what Ansible will be given."""
    return paginate(edges.list_edges(), Edge.from_domain)


@router.get("/v3/edges/{name}", response_model=Edge, responses=NOT_FOUND_RESPONSES)
def get_edge(name: str, edges: FromDishka[EdgeOperationsService]) -> Edge:
    return Edge.from_domain(edges.get_edge(name))


@router.post(
    "/v3/edges",
    response_model=Edge,
    status_code=status.HTTP_201_CREATED,
    responses={**CONFLICT_RESPONSES, **BODY_RESPONSES},
)
def add_edge(
    edge: Edge, operator: OperatorDependency, edges: FromDishka[EdgeOperationsService]
) -> Edge:
    """Register an edge without converging or contacting it."""
    return Edge.from_domain(edges.add_edge(edge.to_domain(), operator))


@router.patch(
    "/v3/edges/{name}",
    response_model=Edge,
    responses={**NOT_FOUND_RESPONSES, **CONFLICT_RESPONSES, **BODY_RESPONSES},
)
def update_edge(
    name: str,
    patch: EdgePatch,
    operator: OperatorDependency,
    edges: FromDishka[EdgeOperationsService],
) -> Edge:
    return Edge.from_domain(edges.update_edge(name, patch.to_domain(), operator))


@router.delete(
    "/v3/edges/{name}",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={202: ACCEPTED_RESPONSE, **NOT_FOUND_RESPONSES, **CONFLICT_RESPONSES},
)
def remove_edge(
    response: Response,
    name: str,
    operator: OperatorDependency,
    edges: FromDishka[EdgeOperationsService],
    queued_operations: FromDishka[OperationService],
    idempotency: IdempotencyKeyDependency,
    keys: FromDishka[IdempotencyKeyStore],
    decommission: Annotated[
        bool,
        Query(
            description=(
                "Strip BlitzeCDN configuration and TLS private keys from the "
                "host before deregistering it."
            ),
        ),
    ] = True,
    force: Annotated[
        bool,
        Query(
            description=(
                "Deregister even if the teardown failed. For a host that no "
                "longer exists; on one that is merely unreachable this leaves "
                "its configuration and private keys in place."
            ),
        ),
    ] = False,
) -> Operation:
    """Queue tearing the host down, then deregistering it, in that order.

    The result is ``{name, decommissioned, hosts}``. A failed teardown fails the
    operation and keeps the edge registered unless ``force`` was given.
    """

    def submit() -> Job:
        edges.get_edge(name)
        return queued_operations.submit(
            EdgeRemovalJob(
                operator=operator, name=name, decommission=decommission, force=force
            ),
            dedupe_key=remove_dedupe_key(name),
        )

    queued = as_queued(
        idempotent(idempotency, keys, create=submit, replay=queued_operations.get)
    )
    polling_headers(response, active=True, location=f"/v3/operations/{queued.id}")
    return queued
