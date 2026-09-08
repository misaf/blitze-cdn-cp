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
    """Rule access for certificate activation and automatic SSL upgrades.

    Writeback targets the rule overrides when a rule produced the derived host;
    otherwise the DNS service updates the zone."""

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
    """Read-only access to derived virtual hosts for installed packages.

    Changing a host requires editing its source zone, rule, or record."""

    def list_sites(self) -> list[CdnSite]: ...

    def get_site(self, name: str) -> CdnSite: ...


class ZoneEditor(Protocol):
    """Desired-state validation required by deployments before convergence."""

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
