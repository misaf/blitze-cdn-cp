"""How the GeoIP slice is built, and what it renders and runs.

Rendering reads the database store, never the service: a release pins the
database the controller holds when it is compiled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.capabilities.security.geoip.adapters import (
    GeoIpDatabaseStore,
    MaxMindDownload,
)
from blitzecdn.capabilities.security.geoip.domain import DATABASE_SHA256_VARIABLE
from blitzecdn.capabilities.security.geoip.ports import (
    DatabaseStore,
    DeploymentLock,
    DeploymentRequirement,
    ReleaseHistory,
)
from blitzecdn.capabilities.security.geoip.service import GeoIpService
from blitzecdn.capabilities.security.geoip.settings import GeoIpConfig
from blitzecdn.core.features import (
    FeatureOperations,
    FeatureRendering,
    FleetStateContribution,
    ScheduledJob,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from pathlib import Path

    from blitzecdn.core.domain.site import CdnSite

__all__ = [
    "DATABASE_DIRECTORY",
    "build_geoip_service",
    "build_geoip_store",
    "geoip_operations",
    "geoip_rendering",
]


#: Under the controller's state directory; the edge role's
#: `blitzecdn_geoip_controller_directory` names the same place. Backed up whole
#: by the backup capability, because MaxMind serves only the newest database
#: and a rollback needs the exact file its release pinned.
DATABASE_DIRECTORY = "geoip-databases"


def build_geoip_store(*, state_dir: Path) -> GeoIpDatabaseStore:
    """The controller's databases, under its own state directory."""
    return GeoIpDatabaseStore(state_dir / DATABASE_DIRECTORY)


def build_geoip_service(
    *,
    config: GeoIpConfig,
    store: DatabaseStore,
    lock: DeploymentLock,
    require_deployment: DeploymentRequirement,
    releases: ReleaseHistory,
) -> GeoIpService:
    """Wire the one download a fleet makes, if this controller has credentials."""
    source = (
        MaxMindDownload(account_id=config.account_id, license_key=config.license_key)
        if config.credentials_configured
        else None
    )
    return GeoIpService(
        store=store,
        source=source,
        lock=lock,
        require_deployment=require_deployment,
        releases=releases,
    )


def geoip_rendering(*, store: DatabaseStore) -> FeatureRendering[CdnSite]:
    """Pin the controller's current database, by digest, into a release."""

    def fleet_state(_sites: tuple[CdnSite, ...]) -> FleetStateContribution | None:
        database = store.current()
        if database is None:
            return None
        return FleetStateContribution(
            feature="geoip",
            variables={DATABASE_SHA256_VARIABLE: database.sha256},
        )

    return FeatureRendering(feature="geoip", fleet_state=fleet_state)


def geoip_operations(*, geoip: GeoIpService, config: GeoIpConfig) -> FeatureOperations:
    """The fleet's one scheduled GeoLite2 download, on the controller."""
    if not geoip.configured:
        return FeatureOperations(feature="geoip")

    def refresh(operator: str) -> None:
        geoip.refresh(operator)

    return FeatureOperations(
        feature="geoip",
        scheduled_jobs=(
            ScheduledJob(
                feature="geoip",
                name="geoip-refresh",
                interval_seconds=config.refresh_interval_seconds,
                run=refresh,
                jitter_seconds=min(3600, config.refresh_interval_seconds // 10),
            ),
        ),
    )
