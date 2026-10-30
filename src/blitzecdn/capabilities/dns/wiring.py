"""How the DNS capability is built, and the flat site document it renders."""

from __future__ import annotations

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.dns.ports import (
    RuleOverrides,
    RuleReader,
    RuleStore,
    ZoneReader,
    ZoneStore,
)
from blitzecdn.capabilities.dns.service import DnsService, HostService, RuleService
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.features import FeatureRendering, SiteStateContribution
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder

__all__ = [
    "RENDERING",
    "build_dns_service",
    "build_host_service",
    "build_rule_service",
    "site_contract",
]


def build_dns_service(
    *, zones: ZoneStore, rules: RuleReader, events: EventRecorder, uow: UnitOfWork
) -> DnsService:
    """Wire zone and record editing, with read-only rules for validation."""
    return DnsService(zones=zones, rules=rules, events=events, uow=uow)


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
    return SiteStateContribution(feature="dns", variables=site_to_ansible(site))


RENDERING = FeatureRendering(feature="dns", site_state=site_contract)
