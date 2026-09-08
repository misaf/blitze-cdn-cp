"""The zone, the policy it is served by, the rules that bend it, and its records.

Five modules, named for what they hold rather than for the layer they sit in.
`zone.py` is a delegated domain together with the policy every hostname in it
is served by; `patch.py` is one request to change that policy; `rule.py` is one
override on it for some of its hostnames; `resolution.py` is what the two
become for one hostname; `record.py` is one answer inside the zone — an address
of its own, or the name of the site that answers for its hostname — with the
partial update that changes one.

They have different lifetimes and different invariants, which is the whole
reason they are separate: a zone is delegated once and configured thereafter,
a rule is written against a zone that already exists, and a record is created,
repointed and deleted against the zone that holds it. The package is the
capability's public face, so `dns.domain` still means what it did when it was
a file.
"""

from blitzecdn.capabilities.dns.domain.patch import DomainPatch
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
    "DnsRecord",
    "Domain",
    "DomainPatch",
    "RecordPatch",
    "RecordType",
    "ResolvedPolicy",
    "Rule",
    "RulePatch",
    "resolve_policy",
]
