from __future__ import annotations

from typing import Protocol

from blitzecdn.capabilities.sites.domain import DnsRecord, Domain, RecordType, Rule
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.operations import EventRecorder


class ZoneStore(Protocol):
    """Zones and the records in them."""

    def list_domains(self) -> list[Domain]:
        """Every zone, ordered by name."""
        ...

    def get_domain(self, name: str) -> Domain:
        """One zone; raises ``NotFoundError`` if it is not served."""
        ...

    def create_domain(self, domain: Domain) -> Domain:
        """Store a new zone; raises ``ConflictError`` if the name is taken."""
        ...

    def replace_domain(
        self, domain: Domain, *, expected: Domain | None = None
    ) -> Domain:
        """Write a zone's policy. The name is its identity and never moves.

        Pass ``expected``, the value the change was merged onto, to make it a
        compare-and-swap that raises ``ConflictError`` if the row moved since; it
        must then run inside a transaction. Raises ``NotFoundError`` if absent.
        """
        ...

    def delete_domain(self, name: str) -> None:
        """Remove a zone with its records and rules; ``NotFoundError`` if absent."""
        ...

    def list_records(self, domain: str | None = None) -> list[DnsRecord]:
        """Every record, or only ``domain``'s when one is named."""
        ...

    def get_record(self, domain: str, name: str, type_: RecordType) -> DnsRecord:
        """One record by its key; raises ``NotFoundError`` if there is none."""
        ...

    def create_record(self, record: DnsRecord) -> DnsRecord:
        """Store a new record.

        Raises ``NotFoundError`` if its zone is not served and ``ConflictError`` if a
        record with the same name and type already exists there.
        """
        ...

    def replace_record(
        self, record: DnsRecord, *, expected: DnsRecord | None = None
    ) -> DnsRecord:
        """Write a record in place; its domain, name and type are its key.

        Pass ``expected``, the value the change was merged onto, to make it a
        compare-and-swap that raises ``ConflictError`` if the row moved since; it
        must then run inside a transaction. Raises ``NotFoundError`` if absent.
        """
        ...

    def delete_record(self, domain: str, name: str, type_: RecordType) -> None:
        """Remove one record; raises ``NotFoundError`` if there is none."""
        ...

    def replace_all_records(
        self, domains: list[Domain], records: list[DnsRecord]
    ) -> None:
        """Make ``domains`` and ``records`` the whole zone state, for a rollback.

        Everything not among them is removed, rules with their zones.
        """
        ...


class RuleReader(Protocol):
    """Read-only rule access for resolution."""

    def list_rules(self, domain: str | None = None) -> list[Rule]:
        """Every rule, or only ``domain``'s when one is named."""
        ...


class RuleOverrides(RuleReader, Protocol):
    """Rule access for writing certificate and automatic SSL results back to a rule."""

    def get_rule(self, domain: str, name: str) -> Rule:
        """One rule; raises ``NotFoundError`` if there is none."""
        ...

    def replace_rule(self, rule: Rule, *, expected: Rule | None = None) -> Rule:
        """Write a rule in place; its domain and name are its key.

        Pass ``expected``, the value the change was merged onto, to make it a
        compare-and-swap that raises ``ConflictError`` if the row moved since; it
        must then run inside a transaction. Raises ``NotFoundError`` if absent.
        """
        ...


class RuleStore(RuleOverrides, Protocol):
    """The overrides, and the writes the rule editor makes to them."""

    def create_rule(self, rule: Rule) -> Rule:
        """Store a new rule; raises ``ConflictError`` if the name is taken."""
        ...

    def delete_rule(self, domain: str, name: str) -> None:
        """Remove one rule; raises ``NotFoundError`` if there is none."""
        ...

    def replace_all_rules(self, rules: list[Rule]) -> None:
        """Make ``rules`` the complete rule set, removing any rule not among them."""
        ...


class ZoneReader(Protocol):
    """The zone a rule overrides, read and never written.

    The editor checks it exists, to name a missing zone clearly; the resolver needs
    its policy.
    """

    def get_domain(self, name: str) -> Domain:
        """One zone; raises ``NotFoundError`` if it is not served."""
        ...


class SiteReader(Protocol):
    """Read-only derived virtual hosts; change one through its zone, rule or record."""

    def list_sites(self) -> list[CdnSite]:
        """Every virtual host, derived from one snapshot of zones, rules and records."""
        ...

    def get_site(self, name: str) -> CdnSite:
        """One derived virtual host; raises ``NotFoundError`` if none has that name."""
        ...


class ZoneEditor(Protocol):
    """Desired-state validation required by deployments before convergence."""

    def validation_errors(self) -> list[str]:
        """Contradictions in canonical state, checked before a deploy converges any.

        Empty when the state is coherent; each entry is a sentence for an operator.
        """
        ...


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
