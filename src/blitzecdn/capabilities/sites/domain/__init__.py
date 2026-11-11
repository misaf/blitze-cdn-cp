"""Canonical zones, records, rules, and the virtual hosts derived from them.

Shared serving policy lives in `core.domain`, so compilation and execution consume
it without importing DNS lifecycle concerns.
"""

from blitzecdn.capabilities.sites.domain.changes import SiteChange
from blitzecdn.capabilities.sites.domain.commands import (
    PatchPolicy,
    PolicyCommand,
)
from blitzecdn.capabilities.sites.domain.hosts import (
    UnservableHost,
    derive_hosts,
    unservable_hosts,
)
from blitzecdn.capabilities.sites.domain.patch import (
    DomainPatch,
    reject_issuer_owned_certificate,
)
from blitzecdn.capabilities.sites.domain.publication import (
    PUBLICATION,
    DnsPublication,
    PublicationMode,
)
from blitzecdn.capabilities.sites.domain.record import (
    DnsRecord,
    RecordPatch,
    RecordType,
)
from blitzecdn.capabilities.sites.domain.resolution import (
    ResolvedPolicy,
    merge_overrides,
    resolve_policy,
)
from blitzecdn.capabilities.sites.domain.rule import Rule, RulePatch
from blitzecdn.capabilities.sites.domain.zone import Domain

__all__ = [
    "PUBLICATION",
    "DnsPublication",
    "DnsRecord",
    "Domain",
    "DomainPatch",
    "PatchPolicy",
    "PolicyCommand",
    "PublicationMode",
    "RecordPatch",
    "RecordType",
    "ResolvedPolicy",
    "Rule",
    "RulePatch",
    "SiteChange",
    "UnservableHost",
    "derive_hosts",
    "merge_overrides",
    "reject_issuer_owned_certificate",
    "resolve_policy",
    "unservable_hosts",
]
