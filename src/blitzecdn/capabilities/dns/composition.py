"""How the DNS capability is built.

Every capability builder here follows the same
rule: what a package could have comes from ``platform``, what only a built-in
may have is an explicit argument.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from blitzecdn.capabilities.dns.ports import (
    RuleOverrides,
    RuleStore,
    ZoneReader,
    ZoneStore,
)
from blitzecdn.capabilities.dns.service import DnsService, RuleService

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.composition import ControlPlane

__all__ = ["build_dns_service", "build_rule_service"]


def build_dns_service(
    platform: ControlPlane, *, zones: ZoneStore, rules: RuleOverrides
) -> DnsService:
    """Wire the service that owns zones, their policy, and their records.

    ``rules`` arrives as ``RuleOverrides`` and not the store: this service
    reads every rule in a zone to derive its virtual hosts, and writes back
    only the certificate fields an issuer owns. Creating and deleting rules is
    ``RuleService``'s, and the port is what says so.
    """
    return DnsService(
        zones=zones,
        rules=rules,
        events=platform.events,
        uow=platform.transactions,
    )


def build_rule_service(
    platform: ControlPlane, *, rules: RuleStore, zones: ZoneReader
) -> RuleService:
    """Wire the editor for a zone's per-hostname overrides.

    ``zones`` is the narrowest thing that answers both questions a rule has of
    a zone — does it exist, and what is its policy — and it is a read. Nothing
    in the rule editor writes a zone.
    """
    return RuleService(
        rules=rules,
        zones=zones,
        events=platform.events,
        uow=platform.transactions,
    )
