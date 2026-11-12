from typing import Annotated

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Query, Response, status

from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.edges.api.models import Edge, EdgeCreate, EdgePatch
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
    PRECONDITION_RESPONSES,
)
from blitzecdn.delivery.api.idempotency import (
    IdempotencyKeyDependency,
    idempotent,
)
from blitzecdn.delivery.api.models import Operation, as_queued
from blitzecdn.delivery.api.pagination import Page, paginate
from blitzecdn.delivery.api.preconditions import (
    ETAG_RESPONSE,
    IfMatchHeader,
    if_match,
    tag,
)
from blitzecdn.delivery.api.responses import (
    ACCEPTED_RESPONSE,
    polling_headers,
)

router = APIRouter(
    tags=["edges"],
    route_class=DishkaSyncRoute,
    dependencies=[Depends(require_operator)],
    responses=OPERATOR_RESPONSES,
)


@router.get("/v3/edges", response_model=Page[Edge], operation_id="edges_list")
def list_edges(edges: FromDishka[EdgeOperationsService]) -> Page[Edge]:
    """Every registered edge, which is exactly what Ansible will be given."""
    return paginate(edges.list_edges(), Edge.from_domain)


@router.get(
    "/v3/edges/{name}",
    response_model=Edge,
    responses={200: ETAG_RESPONSE, **NOT_FOUND_RESPONSES},
    operation_id="edges_get",
)
def get_edge(
    response: Response, name: str, edges: FromDishka[EdgeOperationsService]
) -> Edge:
    return Edge.from_domain(tag(response, edges.get_edge(name)))


@router.post(
    "/v3/edges",
    response_model=Edge,
    status_code=status.HTTP_201_CREATED,
    responses={201: ETAG_RESPONSE, **CONFLICT_RESPONSES, **BODY_RESPONSES},
    operation_id="edges_create",
)
def add_edge(
    response: Response,
    edge: EdgeCreate,
    operator: OperatorDependency,
    edges: FromDishka[EdgeOperationsService],
) -> Edge:
    """Register an edge without converging or contacting it."""
    return Edge.from_domain(tag(response, edges.add_edge(edge.to_domain(), operator)))


@router.patch(
    "/v3/edges/{name}",
    response_model=Edge,
    responses={
        200: ETAG_RESPONSE,
        **NOT_FOUND_RESPONSES,
        **CONFLICT_RESPONSES,
        **PRECONDITION_RESPONSES,
        **BODY_RESPONSES,
    },
    operation_id="edges_update",
)
def update_edge(
    response: Response,
    name: str,
    patch: EdgePatch,
    operator: OperatorDependency,
    edges: FromDishka[EdgeOperationsService],
    precondition: IfMatchHeader = None,
) -> Edge:
    updated = edges.update_edge(
        name, patch.to_domain(), operator, if_match=if_match(precondition)
    )
    return Edge.from_domain(tag(response, updated))


@router.delete(
    "/v3/edges/{name}",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        202: ACCEPTED_RESPONSE,
        **NOT_FOUND_RESPONSES,
        **CONFLICT_RESPONSES,
        **PRECONDITION_RESPONSES,
    },
    operation_id="edges_delete",
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
    precondition: IfMatchHeader = None,
) -> Operation:
    """Queue tearing the host down, then deregistering it, in that order.

    The result is ``{name, decommissioned, hosts}``. A failed teardown fails the
    operation and keeps the edge registered unless ``force`` was given.
    """

    def submit() -> Job:
        # Checked when the removal is queued, in the transaction that queues it.
        # A replay of the same key answers from the job.
        return edges.admit_removal(
            name,
            lambda: queued_operations.submit(
                EdgeRemovalJob(
                    operator=operator, name=name, decommission=decommission, force=force
                ),
                dedupe_key=remove_dedupe_key(name),
            ),
            if_match=if_match(precondition),
        )

    queued = as_queued(
        idempotent(idempotency, keys, create=submit, replay=queued_operations.get)
    )
    polling_headers(response, active=True, location=f"/v3/operations/{queued.id}")
    return queued
