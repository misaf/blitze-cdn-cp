from __future__ import annotations

from typing import Protocol

from blitzecdn.core.operation_ports import EventRecorder
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.features.dns.domain import DnsRecord, Domain, RecordType
from blitzecdn.features.sites.domain import CdnSite
from blitzecdn.features.sites.ports import SiteReader
from blitzecdn.features.tls.policy import CertificateMode, SslMode


class SiteProjection(SiteReader, Protocol):
    """The write side of the derived virtual hosts, and only this feature has it.

    `sites` publishes the read side; re-derivation from records is what may
    write, and re-derivation happens here. The two halves are one object at run
    time — the composition root passes the same store to both — but a
    capability handed `platform.sites` gets `SiteReader` and cannot reach past
    it to replace a projection whose source it does not own.
    """

    def replace_all_sites(self, sites: list[CdnSite]) -> None: ...

    def projection_revision(self) -> str | None: ...

    def set_projection_revision(self, revision: str) -> None: ...


class ZoneStore(Protocol):
    """Zones and records — the source of truth sites are derived from."""

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

    def replace_all_records(
        self, domains: list[Domain], records: list[DnsRecord]
    ) -> None: ...


class ZoneEditor(Protocol):
    """What the other services need from the zone editor.

    Four methods out of ``DnsService``'s twenty. Certificate state has to land
    on the *record* rather than on the derived site, so issuing a certificate
    unavoidably reaches back into the zone editor — but only this far.
    """

    def sync_sites(self) -> None: ...

    #: Ways the stored zones and their derived sites contradict each other.
    #: A deploy asks before it converges anything, because the contradictions
    #: are the kind that would otherwise reach an edge as a valid-looking
    #: config serving the wrong site.
    def validation_errors(self) -> list[str]: ...

    def record_for_site(self, site_name: str) -> DnsRecord: ...

    def activate_managed_certificate(
        self, site: CdnSite, mode: CertificateMode
    ) -> CdnSite: ...

    def apply_automatic_ssl_upgrade(
        self, site_name: str, target: SslMode, operator: str
    ) -> CdnSite | None: ...


__all__ = [
    "EventRecorder",
    "SiteProjection",
    "UnitOfWork",
    "ZoneEditor",
    "ZoneStore",
]
