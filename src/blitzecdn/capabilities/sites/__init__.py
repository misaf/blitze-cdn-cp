"""The site catalogue: zones, records, rules and the virtual hosts they derive."""

from blitzecdn.capabilities.sites.adapters.documents import serving_document
from blitzecdn.capabilities.sites.domain import (
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
from blitzecdn.capabilities.sites.domain.hosts import host_source
from blitzecdn.capabilities.sites.ports import (
    RuleReader,
    RuleStore,
    SiteReader,
    ZoneEditor,
    ZoneStore,
)
from blitzecdn.capabilities.sites.service import SiteService

__all__ = [
    "DnsRecord",
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
    "SiteService",
    "ZoneEditor",
    "ZoneStore",
    "derive_hosts",
    "host_source",
    "serving_document",
    "unservable_hosts",
]
