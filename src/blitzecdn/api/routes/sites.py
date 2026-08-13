from fastapi import APIRouter

from blitzecdn.api.dependencies import ControlPlaneDependency, OperatorDependency
from blitzecdn.domain.sites import CdnSite

router = APIRouter()


# Sites are derived from DNS records and are therefore read-only here. Create,
# change, or remove a record instead; the site follows.
@router.get("/v1/sites", response_model=list[CdnSite])
def list_sites(
    _operator: OperatorDependency, control: ControlPlaneDependency
) -> list[CdnSite]:
    return control.dns.list_sites()


@router.get("/v1/sites/{name}", response_model=CdnSite)
def get_site(
    name: str, _operator: OperatorDependency, control: ControlPlaneDependency
) -> CdnSite:
    """The fully resolved policy for one site, as handed to the edges."""
    return control.dns.get_site(name)
