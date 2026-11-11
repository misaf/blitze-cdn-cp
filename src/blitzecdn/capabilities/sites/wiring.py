"""How the sites capability is built, and the flat site document it renders."""

from __future__ import annotations

from dishka import Provider, Scope, alias, provide

from blitzecdn.capabilities.sites.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.sites.adapters.persistence import ZoneStore as ZoneTable
from blitzecdn.capabilities.sites.adapters.rules import RuleStore as RuleTable
from blitzecdn.capabilities.sites.ports import (
    RuleOverrides,
    RuleReader,
    RuleStore,
    SiteReader,
    ZoneEditor,
    ZoneReader,
    ZoneStore,
)
from blitzecdn.capabilities.sites.service import HostService, RuleService, SiteService
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.features import FeatureRendering, SiteStateContribution
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder

__all__ = [
    "RENDERING",
    "SiteProvider",
    "build_host_service",
    "build_rule_service",
    "build_site_service",
    "site_contract",
]


def build_site_service(
    *, zones: ZoneStore, rules: RuleReader, events: EventRecorder, uow: UnitOfWork
) -> SiteService:
    """Wire zone and record editing, with read-only rules for validation."""
    return SiteService(zones=zones, rules=rules, events=events, uow=uow)


def build_host_service(
    *,
    zones: ZoneStore,
    rules: RuleOverrides,
    events: EventRecorder,
    uow: UnitOfWork,
) -> HostService:
    """Wire derived-host reads and writeback, without rule creation or deletion."""
    return HostService(zones=zones, rules=rules, events=events, uow=uow)


def build_rule_service(
    *, rules: RuleStore, zones: ZoneReader, events: EventRecorder, uow: UnitOfWork
) -> RuleService:
    """Wire the rule editor; it reads zones and never writes one."""
    return RuleService(
        rules=rules,
        zones=zones,
        events=events,
        uow=uow,
    )


def site_contract(site: CdnSite) -> SiteStateContribution:
    """Project the stable flat site contract consumed by the edge roles."""
    return SiteStateContribution(feature="sites", variables=site_to_ansible(site))


RENDERING = FeatureRendering(feature="sites", site_state=site_contract)


class SiteProvider(Provider):
    """The zone and rule tables, and the three services that edit them."""

    scope = Scope.APP

    @provide
    def zones(self, database: Database) -> ZoneStore:
        return ZoneTable(database)

    @provide
    def rules(self, database: Database) -> RuleStore:
        return RuleTable(database)

    @provide
    def site_service(
        self, zones: ZoneStore, rules: RuleStore, events: EventRecorder, uow: UnitOfWork
    ) -> SiteService:
        return build_site_service(zones=zones, rules=rules, events=events, uow=uow)

    @provide
    def hosts(
        self, zones: ZoneStore, rules: RuleStore, events: EventRecorder, uow: UnitOfWork
    ) -> HostService:
        return build_host_service(zones=zones, rules=rules, events=events, uow=uow)

    @provide
    def rule_editor(
        self, rules: RuleStore, zones: ZoneStore, events: EventRecorder, uow: UnitOfWork
    ) -> RuleService:
        # A rule may consult the zone policy it overrides and may never write one.
        return build_rule_service(rules=rules, zones=zones, events=events, uow=uow)

    rule_reader = alias(source=RuleStore, provides=RuleReader)
    sites = alias(source=HostService, provides=SiteReader)
    zone_editor = alias(source=SiteService, provides=ZoneEditor)
