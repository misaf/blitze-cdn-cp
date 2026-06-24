from fastapi import BackgroundTasks, FastAPI, HTTPException, status

from app.schemas import CdnSite, CdnSiteCreate, CdnSiteUpdate, DeploymentResult
from app.services.ansible_runner import run_ansible_playbook
from app.services.cdn_sites import delete_site, list_sites, save_site, update_site

app = FastAPI(title="Blitze CDN API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/cdn-sites", response_model=list[CdnSite])
def get_cdn_sites() -> list[CdnSite]:
    return list_sites()


@app.post("/cdn-sites", response_model=CdnSite, status_code=status.HTTP_201_CREATED)
def create_cdn_site(site: CdnSiteCreate) -> CdnSite:
    try:
        return save_site(site)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@app.patch("/cdn-sites/{name}", response_model=CdnSite)
def patch_cdn_site(name: str, update: CdnSiteUpdate) -> CdnSite:
    try:
        return update_site(name, update)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@app.delete("/cdn-sites/{name}", status_code=status.HTTP_204_NO_CONTENT)
def remove_cdn_site(name: str) -> None:
    try:
        delete_site(name)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@app.post("/deploy", response_model=DeploymentResult)
def deploy(background_tasks: BackgroundTasks, wait: bool = False) -> DeploymentResult:
    if wait:
        return run_ansible_playbook()

    background_tasks.add_task(run_ansible_playbook)
    return DeploymentResult(started=True, returncode=None, stdout="", stderr="")
