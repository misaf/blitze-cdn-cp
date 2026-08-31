"""DNS and derived-site public contracts."""

from blitzecdn.features.dns.domain import DnsRecord, Domain, RecordPatch, RecordType
from blitzecdn.features.dns.service import DnsService
from blitzecdn.features.dns.site_domain import (
    CacheQueryStringMode,
    CdnSite,
    CertificateMode,
    HttpScheme,
    SitePolicy,
    SslMode,
)

__all__ = [
    "CacheQueryStringMode",
    "CdnSite",
    "CertificateMode",
    "DnsRecord",
    "DnsService",
    "Domain",
    "HttpScheme",
    "RecordPatch",
    "RecordType",
    "SitePolicy",
    "SslMode",
]
