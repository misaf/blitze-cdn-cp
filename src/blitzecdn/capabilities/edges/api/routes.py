from typing import Annotated

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaSyncRoute
from fastapi import APIRouter, Depends, Query, Response, status

from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.edges.api.models import Edge, EdgePatch
from blitzecdn.capabilities.edges.service.operations import (
    REMOVE_OPERATION,
    remove_dedupe_key,
    remove_payload,
)
from blitzecdn.capabilities.jobs import OperationService
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.models import Operation, as_queued
from blitzecdn.entrypoints.api.pagination import Page, paginate
from blitzecdn.entrypoints.api.responses import (
    ACCEPTED_RESPONSE,
    polling_headers,
)

router = APIRouter(
    route_class=DishkaSyncRoute, dependencies=[Depends(require_operator)]
)


@router.get("/v3/edges", response_model=Page[Edge])
def list_edges(edges: FromDishka[EdgeOperationsService]) -> Page[Edge]:
    """Every registered edge, which is exactly what Ansible will be given."""
    return paginate(edges.list_edges(), Edge.from_domain)


@router.get("/v3/edges/{name}", response_model=Edge)
def get_edge(name: str, edges: FromDishka[EdgeOperationsService]) -> Edge:
    return Edge.from_domain(edges.get_edge(name))


@router.post("/v3/edges", response_model=Edge, status_code=status.HTTP_201_CREATED)
def add_edge(
    edge: Edge, operator: OperatorDependency, edges: FromDishka[EdgeOperationsService]
) -> Edge:
    """Register an edge without converging or contacting it."""
    return Edge.from_domain(edges.add_edge(edge.to_domain(), operator))


@router.patch("/v3/edges/{name}", response_model=Edge)
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
    responses={202: ACCEPTED_RESPONSE},
)
def remove_edge(
    response: Response,
    name: str,
    operator: OperatorDependency,
    edges: FromDishka[EdgeOperationsService],
    queued_operations: FromDishka[OperationService],
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
    edges.get_edge(name)
    queued = as_queued(
        queued_operations.submit(
            REMOVE_OPERATION,
            remove_payload(operator, name=name, decommission=decommission, force=force),
            dedupe_key=remove_dedupe_key(name),
        )
    )
    polling_headers(response, active=True, location=f"/v3/operations/{queued.id}")
    return queued
