"""DNS zone, record, and service public contracts."""

from blitzecdn.capabilities.dns.adapters.documents import serving_document
from blitzecdn.capabilities.dns.domain import (
    DnsRecord,
    Domain,
    DomainPatch,
    PatchPolicy,
    RecordPatch,
    RecordType,
    Rule,
    SiteChange,
    derive_hosts,
    unservable_hosts,
)
from blitzecdn.capabilities.dns.domain.hosts import host_source
from blitzecdn.capabilities.dns.ports import (
    RuleReader,
    RuleStore,
    SiteReader,
    ZoneEditor,
    ZoneStore,
)
from blitzecdn.capabilities.dns.service import DnsService

__all__ = [
    "DnsRecord",
    "DnsService",
    "Domain",
    "DomainPatch",
    "PatchPolicy",
    "RecordPatch",
    "RecordType",
    "Rule",
    "RuleReader",
    "RuleStore",
    "SiteChange",
    "SiteReader",
    "ZoneEditor",
    "ZoneStore",
    "derive_hosts",
    "host_source",
    "serving_document",
    "unservable_hosts",
]
