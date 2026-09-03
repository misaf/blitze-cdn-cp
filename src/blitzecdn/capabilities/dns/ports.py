from __future__ import annotations

from typing import Protocol

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, RecordType
from blitzecdn.capabilities.sites.ports import SiteReader
from blitzecdn.core.operation_ports import EventRecorder
from blitzecdn.core.ports import UnitOfWork


class SiteHostnames(SiteReader, Protocol):
    """The one part of a site this capability writes, and the reads it needs.

    A site is canonical and `sites` owns it. What `dns` owns is the answer to
    "which hostnames route here", because that answer *is* the set of records
    pointing at the site — so ``server_names`` is maintained from this side and
    from nowhere else, and the site's own service has no method that sets it.

    The read half comes along because the checks this capability runs before a
    deploy are cross-cutting: whether a record names a site that exists, and
    whether the hostnames stored on each site still match the records. Both
    need to see sites; neither may change one.
    """

    def set_server_names(self, site: str, server_names: tuple[str, ...]) -> None: ...

    def projection_revision(self) -> str | None: ...

    def set_projection_revision(self, revision: str) -> None: ...


class ZoneStore(Protocol):
    """Zones and the records in them."""

    def list_domains(self) -> list[Domain]: ...

    def get_domain(self, name: str) -> Domain: ...

    def create_domain(self, domain: Domain) -> Domain: ...

    def delete_domain(self, name: str) -> None: ...

    def list_records(self, domain: str | None = None) -> list[DnsRecord]: ...

    def get_record(self, domain: str, name: str, type_: RecordType) -> DnsRecord: ...

    def create_record(self, record: DnsRecord) -> DnsRecord: ...

    def replace_record(
        self, record: DnsRecord, *, expected: DnsRecord | None = None
    ) -> DnsRecord: ...

    def delete_record(self, domain: str, name: str, type_: RecordType) -> None: ...

    def delete_all_records(self) -> None: ...

    def replace_all_records(
        self, domains: list[Domain], records: list[DnsRecord]
    ) -> None: ...


class ZoneEditor(Protocol):
    """What `deployments` needs from the zone editor, and it is now two things.

    It used to be four. Two of them — ``activate_managed_certificate`` and
    ``apply_automatic_ssl_upgrade`` — were certificate state reaching back into
    a record because the derived site could not hold it. The site holds it now,
    so both moved to ``SiteService`` and this port lost the reason it had to
    know what a certificate is.
    """

    def resync_hostnames(self) -> None: ...

    #: Ways canonical state contradicts itself. A deploy asks before it
    #: converges anything, because the contradictions are the kind that would
    #: otherwise reach an edge as a valid-looking config serving the wrong site.
    def validation_errors(self) -> list[str]: ...


__all__ = [
    "EventRecorder",
    "SiteHostnames",
    "UnitOfWork",
    "ZoneEditor",
    "ZoneStore",
]
