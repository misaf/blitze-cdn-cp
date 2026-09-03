from fastapi import APIRouter, Depends, status

from blitzecdn.api.dependencies import (
    ControlPlaneDependency,
    OperatorDependency,
    require_operator,
)
from blitzecdn.api.models import CdnSite, CdnSiteCreate, SitePatch

router = APIRouter(dependencies=[Depends(require_operator)])


@router.get("/v1/sites", response_model=list[CdnSite])
def list_sites(control: ControlPlaneDependency) -> list[CdnSite]:
    return [CdnSite.from_domain(item) for item in control.sites.list_sites()]


@router.post("/v1/sites", response_model=CdnSite, status_code=status.HTTP_201_CREATED)
def create_site(
    site: CdnSiteCreate,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> CdnSite:
    """Add a site. It serves nothing until a record routes a hostname to it."""
    return CdnSite.from_domain(
        control.site_editor.create_site(site.to_domain(), operator)
    )


@router.get("/v1/sites/{name}", response_model=CdnSite)
def get_site(name: str, control: ControlPlaneDependency) -> CdnSite:
    """The fully resolved policy for one site, as handed to the edges."""
    return CdnSite.from_domain(control.sites.get_site(name))


@router.patch("/v1/sites/{name}", response_model=CdnSite)
def update_site(
    name: str,
    patch: SitePatch,
    operator: OperatorDependency,
    control: ControlPlaneDependency,
) -> CdnSite:
    return CdnSite.from_domain(
        control.site_editor.update_site(name, patch.to_domain(), operator)
    )


@router.delete("/v1/sites/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_site(
    name: str, operator: OperatorDependency, control: ControlPlaneDependency
) -> None:
    """Refused while records still route hostnames here."""
    control.site_editor.delete_site(name, operator)
