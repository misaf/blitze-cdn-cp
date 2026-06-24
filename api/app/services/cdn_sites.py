from pathlib import Path

import yaml

from app.schemas import CdnSite, CdnSiteCreate, CdnSiteUpdate
from app.settings import ANSIBLE_VARS_FILE, SITES_FILE


def list_sites() -> list[CdnSite]:
    data = _read_yaml(SITES_FILE)
    return [CdnSite.model_validate(item) for item in data.get("nginx_cdn_sites", [])]


def save_site(site: CdnSiteCreate) -> CdnSite:
    sites = list_sites()
    if any(existing.name == site.name for existing in sites):
        raise ValueError(f"CDN site '{site.name}' already exists")

    created = CdnSite.model_validate(site.model_dump())
    _write_sites([*sites, created])
    return created


def update_site(name: str, update: CdnSiteUpdate) -> CdnSite:
    sites = list_sites()
    for index, site in enumerate(sites):
        if site.name != name:
            continue

        values = site.model_dump()
        values.update(update.model_dump(exclude_unset=True))
        updated = CdnSite.model_validate(values)
        sites[index] = updated
        _write_sites(sites)
        return updated

    raise KeyError(f"CDN site '{name}' was not found")


def delete_site(name: str) -> None:
    sites = list_sites()
    remaining = [site for site in sites if site.name != name]
    if len(remaining) == len(sites):
        raise KeyError(f"CDN site '{name}' was not found")

    _write_sites(remaining)


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {"nginx_cdn_sites": []}

    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {"nginx_cdn_sites": []}


def _write_sites(sites: list[CdnSite]) -> None:
    payload = {"nginx_cdn_sites": [_ansible_site(site) for site in sites]}
    _write_yaml(SITES_FILE, payload)
    _write_yaml(ANSIBLE_VARS_FILE, payload)


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        yaml.safe_dump(payload, file, sort_keys=False)


def _ansible_site(site: CdnSite) -> dict:
    data = site.model_dump(exclude_none=True)
    if not data["cache_enabled"]:
        data["cache_valid_success"] = "0s"
        data["cache_valid_not_found"] = "0s"
    return data
