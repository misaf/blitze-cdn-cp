from fastapi import APIRouter, Query, status

from blitzecdn.api.dependencies import ControlPlaneDependency, OperatorDependency
from blitzecdn.api_models import EdgeRemoval, OriginCheckRequest
from blitzecdn.domain.edges import Edge, EdgePatch
from blitzecdn.domain.origins import OriginReport

router = APIRouter()


@router.get("/v1/edges", response_model=list[Edge])
def list_edges(
    _operator: OperatorDependency, control: ControlPlaneDependency
) -> list[Edge]:
    """Every registered edge, which is exactly what Ansible will be given."""
    return control.edges.list_edges()


@router.get("/v1/edges/{name}", response_model=Edge)
def get_edge(
    name: str, _operator: OperatorDependency, control: ControlPlaneDependency
) -> Edge:
    return control.edges.get_edge(name)


@router.post("/v1/edges", response_model=Edge, status_code=status.HTTP_201_CREATED)
def add_edge(
    edge: Edge, operator: OperatorDependency, control: ControlPlaneDependency
) -> Edge:
    """Register an edge without converging or contacting it."""
    return control.edges.add_edge(edge, operator)


@router.patch("/v1/edges/{name}", response_model=Edge)
def update_edge(
    name: str,
    patch: EdgePatch,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> Edge:
    return control.edges.update_edge(name, patch, operator)


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
    return EdgeRemoval(name=name, decommissioned=True, hosts=hosts)


@router.post("/v1/origins/check", response_model=OriginReport)
def check_origins(
    request: OriginCheckRequest,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> OriginReport:
    """Ask the edges to connect to the origins they proxy to."""
    return control.edges.check_origins(operator, host_limit=request.host_limit)
