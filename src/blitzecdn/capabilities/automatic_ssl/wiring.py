"""How Automatic SSL/TLS is built, and the scan it schedules."""

from __future__ import annotations

from dishka import Provider, Scope, provide

from blitzecdn.capabilities.automatic_ssl.ports import SslModeUpgrades
from blitzecdn.capabilities.automatic_ssl.service import AutomaticSslService
from blitzecdn.capabilities.automatic_ssl.settings import (
    CONFIGURATION,
    AutomaticSslConfig,
)
from blitzecdn.capabilities.deployments import DeploymentGateway
from blitzecdn.capabilities.dns import SiteReader
from blitzecdn.capabilities.edges import OriginProbe
from blitzecdn.capabilities.origins import OriginCheckRunner
from blitzecdn.core.features import FeatureOperations, ScheduledJob
from blitzecdn.core.features.resolution import ResolvedCapabilityEnvironment

__all__ = [
    "AutomaticSslProvider",
    "automatic_ssl_operations",
    "build_automatic_ssl_service",
]


def build_automatic_ssl_service(
    *,
    sites: SiteReader,
    origin_checks: OriginCheckRunner,
    origin_probe: OriginProbe,
    upgrades: SslModeUpgrades,
    deployments: DeploymentGateway,
) -> AutomaticSslService:
    """Wire upgrades that apply only what both origin transports prove."""
    return AutomaticSslService(
        sites=sites,
        runner=origin_checks,
        origin_probe=origin_probe,
        site_editor=upgrades,
        deployments=deployments,
    )


def automatic_ssl_operations(
    *, automatic_ssl: AutomaticSslService, config: AutomaticSslConfig
) -> FeatureOperations:
    """The periodic origin scan."""

    def scan(operator: str) -> None:
        automatic_ssl.reconcile(operator)

    return FeatureOperations(
        feature="automatic_ssl",
        scheduled_jobs=(
            ScheduledJob(
                feature="automatic_ssl",
                name="automatic-ssl-scan",
                interval_seconds=config.scan_interval_seconds,
                run=scan,
                jitter_seconds=min(86_400, config.scan_interval_seconds // 30),
            ),
        ),
    )


class AutomaticSslProvider(Provider):
    """The scan's configuration and service."""

    scope = Scope.APP

    @provide
    def config(
        self, capability_config: ResolvedCapabilityEnvironment
    ) -> AutomaticSslConfig:
        return capability_config.for_feature(CONFIGURATION.feature).as_model(
            AutomaticSslConfig
        )

    @provide
    def service(
        self,
        sites: SiteReader,
        origin_checks: OriginCheckRunner,
        origin_probe: OriginProbe,
        upgrades: SslModeUpgrades,
        deployments: DeploymentGateway,
    ) -> AutomaticSslService:
        return build_automatic_ssl_service(
            sites=sites,
            origin_checks=origin_checks,
            origin_probe=origin_probe,
            upgrades=upgrades,
            deployments=deployments,
        )
