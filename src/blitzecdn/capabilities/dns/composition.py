"""How the DNS capability is built.

Every collaborator is an explicit argument. A builder never receives the
composition root, so what a service depends on is its signature.
"""

from __future__ import annotations

from blitzecdn.capabilities.dns.ports import (
    RuleOverrides,
    RuleReader,
    RuleStore,
    ZoneReader,
    ZoneStore,
)
from blitzecdn.capabilities.dns.service import DnsService, HostService, RuleService
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder

__all__ = ["build_dns_service", "build_host_service", "build_rule_service"]


def build_dns_service(
    *, zones: ZoneStore, rules: RuleReader, events: EventRecorder, uow: UnitOfWork
) -> DnsService:
    """Wire canonical zone and record editing and validation.

    ``rules`` arrives as the reader rather than the store. Validating desired
    state means resolving hostnames through the overrides exactly as a
    deployment will, and the zone editor writes none of them.
    """
    return DnsService(zones=zones, rules=rules, events=events, uow=uow)


def build_host_service(
    *,
    zones: ZoneStore,
    rules: RuleOverrides,
    events: EventRecorder,
    uow: UnitOfWork,
) -> HostService:
    """Wire derived-host reads and source-policy writeback.

    RuleOverrides permits updating an existing rule without giving this service
    ownership of rule creation or deletion.
    """
    return HostService(zones=zones, rules=rules, events=events, uow=uow)


def build_rule_service(
    *, rules: RuleStore, zones: ZoneReader, events: EventRecorder, uow: UnitOfWork
) -> RuleService:
    """Wire the editor for a zone's per-hostname overrides.

    ``zones`` is the narrowest thing that answers both questions a rule has of
    a zone — does it exist, and what is its policy — and it is a read. Nothing
    in the rule editor writes a zone.
    """
    return RuleService(
        rules=rules,
        zones=zones,
        events=events,
        uow=uow,
    )
