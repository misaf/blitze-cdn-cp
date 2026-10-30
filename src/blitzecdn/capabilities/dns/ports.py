from __future__ import annotations

from typing import Protocol

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, RecordType, Rule
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder


class ZoneStore(Protocol):
    """Zones and the records in them."""

    def list_domains(self) -> list[Domain]: ...

    def get_domain(self, name: str) -> Domain: ...

    def create_domain(self, domain: Domain) -> Domain: ...

    def replace_domain(
        self, domain: Domain, *, expected: Domain | None = None
    ) -> Domain: ...

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
    """Read-only rule access for resolution."""

    def list_rules(self, domain: str | None = None) -> list[Rule]: ...


class RuleOverrides(RuleReader, Protocol):
    """Rule access for writing certificate and automatic SSL results back to a rule."""

    def get_rule(self, domain: str, name: str) -> Rule: ...

    def replace_rule(self, rule: Rule, *, expected: Rule | None = None) -> Rule: ...


class RuleStore(RuleOverrides, Protocol):
    """The overrides, and the writes the rule editor makes to them."""

    def create_rule(self, rule: Rule) -> Rule: ...

    def delete_rule(self, domain: str, name: str) -> None: ...

    def replace_all_rules(self, rules: list[Rule]) -> None: ...


class ZoneReader(Protocol):
    """The zone a rule overrides, read and never written.

    The editor checks it exists, to name a missing zone clearly; the resolver needs
    its policy.
    """

    def get_domain(self, name: str) -> Domain: ...


class SiteReader(Protocol):
    """Read-only derived virtual hosts; change one through its zone, rule or record."""

    def list_sites(self) -> list[CdnSite]: ...

    def get_site(self, name: str) -> CdnSite: ...


class ZoneEditor(Protocol):
    """Desired-state validation required by deployments before convergence."""

    #: Contradictions in canonical state, checked before a deploy converges any.
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
