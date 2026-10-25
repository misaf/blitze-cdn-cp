from fastapi import APIRouter, Depends, Query, status

from blitzecdn.capabilities.edges.api.models import Edge, EdgePatch
from blitzecdn.capabilities.edges.service.operations import (
    REMOVE_OPERATION,
    remove_dedupe_key,
    remove_payload,
)
from blitzecdn.entrypoints.api.dependencies import (
    OperatorDependency,
    ServicesDependency,
    require_operator,
)
from blitzecdn.entrypoints.api.models import Operation, as_queued

router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/v1/edges", response_model=list[Edge])
def list_edges(control: ServicesDependency) -> list[Edge]:
    """Every registered edge, which is exactly what Ansible will be given."""
    return [Edge.from_domain(item) for item in control.edges.list_edges()]


@router.get("/v1/edges/{name}", response_model=Edge)
def get_edge(name: str, control: ServicesDependency) -> Edge:
    return Edge.from_domain(control.edges.get_edge(name))


@router.post("/v1/edges", response_model=Edge, status_code=status.HTTP_201_CREATED)
def add_edge(
    edge: Edge, operator: OperatorDependency, control: ServicesDependency
) -> Edge:
    """Register an edge without converging or contacting it."""
    return Edge.from_domain(control.edges.add_edge(edge.to_domain(), operator))


@router.patch("/v1/edges/{name}", response_model=Edge)
def update_edge(
    name: str,
    patch: EdgePatch,
    operator: OperatorDependency,
    control: ServicesDependency,
) -> Edge:
    return Edge.from_domain(
        control.edges.update_edge(name, patch.to_domain(), operator)
    )


@router.delete(
    "/v1/edges/{name}",
    response_model=Operation,
    status_code=status.HTTP_202_ACCEPTED,
)
def remove_edge(
    name: str,
    operator: OperatorDependency,
    control: ServicesDependency,
    decommission: bool = Query(
        True,
        description=(
            "Strip BlitzeCDN configuration and TLS private keys from the "
            "host before deregistering it."
        ),
    ),
    force: bool = Query(
        False,
        description=(
            "Deregister even if the teardown failed. For a host that no "
            "longer exists; on one that is merely unreachable this leaves "
            "its configuration and private keys in place."
        ),
    ),
) -> Operation:
    """Queue tearing the host down, then deregistering it, in that order.

    The result is ``{name, decommissioned, hosts}``. A failed teardown fails the
    operation and keeps the edge registered unless ``force`` was given.
    """
    control.edges.get_edge(name)
    return as_queued(
        control.queued_operations.submit(
            REMOVE_OPERATION,
            remove_payload(operator, name=name, decommission=decommission, force=force),
            dedupe_key=remove_dedupe_key(name),
        )
    )
