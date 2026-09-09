"""The zone, the policy it is served by, its rules, its records, its hosts.

Six modules, named for what they hold rather than for the layer they sit in.
`host.py` composes every capability's contract into ``SitePolicy`` and the
virtual host that carries it; `zone.py` is a delegated domain holding that
policy; `patch.py` is one request to change it; `rule.py` is one override on it
for some of its hostnames; `resolution.py` is what the two become for one
hostname; `hosts.py` turns a whole zone into the virtual hosts an edge serves;
`record.py` is one answer inside the zone, with the partial update that changes
one.

They have different lifetimes and different invariants, which is the whole
reason they are separate: a zone is delegated once and configured thereafter, a
rule is written against a zone that already exists, a record is created,
repointed and deleted against the zone that holds it — and a host is authored
by nobody, being what the other three resolve to.
"""

from blitzecdn.capabilities.dns.domain.host import CdnSite, SitePolicy
from blitzecdn.capabilities.dns.domain.hosts import derive_hosts
from blitzecdn.capabilities.dns.domain.patch import (
    DomainPatch,
    reject_issuer_owned_certificate,
)
from blitzecdn.capabilities.dns.domain.record import (
    DnsRecord,
    RecordPatch,
    RecordType,
)
from blitzecdn.capabilities.dns.domain.resolution import (
    ResolvedPolicy,
    resolve_policy,
)
from blitzecdn.capabilities.dns.domain.rule import Rule, RulePatch
from blitzecdn.capabilities.dns.domain.zone import Domain

__all__ = [
    "CdnSite",
    "DnsRecord",
    "Domain",
    "DomainPatch",
    "RecordPatch",
    "RecordType",
    "ResolvedPolicy",
    "Rule",
    "RulePatch",
    "SitePolicy",
    "derive_hosts",
    "reject_issuer_owned_certificate",
    "resolve_policy",
]
