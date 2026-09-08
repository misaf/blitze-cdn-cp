from __future__ import annotations

from typing import Protocol

from blitzecdn.capabilities.dns.domain import (
    CdnSite,
    DnsRecord,
    Domain,
    RecordType,
    Rule,
)
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder


class ZoneStore(Protocol):
    """Zones and the records in them."""

    def list_domains(self) -> list[Domain]: ...

    def get_domain(self, name: str) -> Domain: ...

    def create_domain(self, domain: Domain) -> Domain: ...

    def replace_domain(self, domain: Domain) -> Domain: ...

    def delete_domain(self, name: str) -> None: ...

    def list_records(self, domain: str | None = None) -> list[DnsRecord]: ...

    def get_record(self, domain: str, name: str, type_: RecordType) -> DnsRecord: ...

    def create_record(self, record: DnsRecord) -> DnsRecord: ...

    def replace_record(
        self, record: DnsRecord, *, expected: DnsRecord | None = None
    ) -> DnsRecord: ...

    def delete_record(self, domain: str, name: str, type_: RecordType) -> None: ...

    def replace_all_records(
        self, domains: list[Domain], records: list[DnsRecord]
    ) -> None: ...


class RuleReader(Protocol):
    """The read a resolver needs, and the only one a reader should be handed.

    Narrower than ``RuleStore`` on purpose: resolving a hostname reads the
    rules of a zone and must never be able to write one.
    """

    def list_rules(self, domain: str | None = None) -> list[Rule]: ...


class RuleOverrides(RuleReader, Protocol):
    """Reading a zone's rules, and writing back the fields an issuer owns.

    The zone editor holds this rather than ``RuleReader`` for one path: a
    managed certificate is recorded against whatever authored object produced
    the virtual host it was issued for, and for a host that came from a rule
    that means the rule's overrides. It is the same narrow arrangement the
    hostname projection used to have — a second writer of one specific fact,
    named by a port so that "who wrote this" stays answerable — except that
    this one writes three fields an operator never sets by hand.
    """

    def get_rule(self, domain: str, name: str) -> Rule: ...

    def replace_rule(self, rule: Rule) -> Rule: ...


class RuleStore(RuleOverrides, Protocol):
    """The overrides, and the writes the rule editor makes to them."""

    def create_rule(self, rule: Rule) -> Rule: ...

    def delete_rule(self, domain: str, name: str) -> None: ...

    def replace_all_rules(self, rules: list[Rule]) -> None: ...


class ZoneReader(Protocol):
    """The zone a rule overrides, read and never written.

    Two callers want it for two reasons. The rule editor wants to know the zone
    exists: a rule for a zone we do not serve is an override that can never
    apply, and the foreign key would refuse it anyway — but as an integrity
    error the operator has to decode rather than as a sentence naming the zone.
    The resolver wants the policy itself, because an override is only half an
    answer; the zone is the other half.

    One read serves both, and the rule editor may do nothing else to a zone.
    """

    def get_domain(self, name: str) -> Domain: ...


class SiteReader(Protocol):
    """The virtual hosts, read-only. What an installed package is handed.

    ``platform.sites`` for a distribution that has to know what the fleet
    serves — which origins to probe, which hostnames need a certificate — and
    it can answer that without being able to write anything at all. There is no
    write side to withhold any more: these are derived, and the way to change
    one is to change the zone, the rule or the record it came from.
    """

    def list_sites(self) -> list[CdnSite]: ...

    def get_site(self, name: str) -> CdnSite: ...


class ZoneEditor(Protocol):
    """What `deployments` needs from the zone editor, and it is now one thing.

    It used to be four. Two — ``activate_managed_certificate`` and
    ``apply_automatic_ssl_upgrade`` — were certificate state reaching into a
    record because the derived site could not hold it; they went to the zone
    with the rest of the policy. ``resync_hostnames`` went with the projection
    it maintained: ``server_names`` is derived from the records at render time
    now, so there is no table to keep in step and nothing to resync.
    """

    #: Ways canonical state contradicts itself. A deploy asks before it
    #: converges anything, because the contradictions are the kind that would
    #: otherwise reach an edge as a valid-looking config serving the wrong site.
    def validation_errors(self) -> list[str]: ...


__all__ = [
    "EventRecorder",
    "RuleOverrides",
    "RuleReader",
    "RuleStore",
    "SiteReader",
    "UnitOfWork",
    "ZoneEditor",
    "ZoneReader",
    "ZoneStore",
]
