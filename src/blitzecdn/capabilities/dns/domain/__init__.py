"""Canonical zones, records, rules, and the virtual hosts derived from them.

Shared serving policy lives in `serving`, so compilation and execution consume
it without importing DNS lifecycle concerns.
"""

from blitzecdn.capabilities.dns.domain.commands import (
    EditFirewall,
    EditVisitorHeaders,
    PatchPolicy,
    PolicyCommand,
)
from blitzecdn.capabilities.dns.domain.hosts import (
    UnservableHost,
    derive_hosts,
    unservable_hosts,
)
from blitzecdn.capabilities.dns.domain.patch import (
    DomainPatch,
    reject_issuer_owned_certificate,
)
from blitzecdn.capabilities.dns.domain.publication import (
    PUBLICATION,
    DnsPublication,
    PublicationMode,
)
from blitzecdn.capabilities.dns.domain.record import (
    DnsRecord,
    RecordPatch,
    RecordType,
)
from blitzecdn.capabilities.dns.domain.resolution import (
    ResolvedPolicy,
    merge_overrides,
    resolve_policy,
)
from blitzecdn.capabilities.dns.domain.rule import Rule, RulePatch
from blitzecdn.capabilities.dns.domain.zone import Domain

__all__ = [
    "PUBLICATION",
    "DnsPublication",
    "DnsRecord",
    "Domain",
    "DomainPatch",
    "EditFirewall",
    "EditVisitorHeaders",
    "PatchPolicy",
    "PolicyCommand",
    "PublicationMode",
    "RecordPatch",
    "RecordType",
    "ResolvedPolicy",
    "Rule",
    "RulePatch",
    "UnservableHost",
    "derive_hosts",
    "merge_overrides",
    "reject_issuer_owned_certificate",
    "resolve_policy",
    "unservable_hosts",
]
