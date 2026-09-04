from fastapi import APIRouter, Depends, Query, status

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    require_operator,
)
from blitzecdn.api.models import HostRun, as_operation
from blitzecdn.capabilities.edges.api.models import Edge, EdgePatch, EdgeRemoval

router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/v1/edges", response_model=list[Edge])
def list_edges(control: ControlPlaneDependency) -> list[Edge]:
    """Every registered edge, which is exactly what Ansible will be given."""
    return [Edge.from_domain(item) for item in control.edges.list_edges()]


@router.get("/v1/edges/{name}", response_model=Edge)
def get_edge(name: str, control: ControlPlaneDependency) -> Edge:
    return Edge.from_domain(control.edges.get_edge(name))


@router.post("/v1/edges", response_model=Edge, status_code=status.HTTP_201_CREATED)
def add_edge(
    edge: Edge, operator: OperatorDependency, control: ControlPlaneDependency
) -> Edge:
    """Register an edge without converging or contacting it."""
    return Edge.from_domain(control.edges.add_edge(edge.to_domain(), operator))


@router.patch("/v1/edges/{name}", response_model=Edge)
def update_edge(
    name: str,
    patch: EdgePatch,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> Edge:
    return Edge.from_domain(
        control.edges.update_edge(name, patch.to_domain(), operator)
    )


@router.delete("/v1/edges/{name}", response_model=EdgeRemoval)
def remove_edge(
    name: str,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
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
) -> EdgeRemoval:
    """Tear the host down, then deregister it — in that order."""
    if not decommission:
        control.edges.remove_edge(name, operator)
        return EdgeRemoval(name=name, decommissioned=False)
    hosts = control.edges.decommission_edge(name, operator, force=force)
    return EdgeRemoval(
        name=name,
        decommissioned=True,
        hosts=tuple(as_operation(host, HostRun) for host in hosts),
    )
