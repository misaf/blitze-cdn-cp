"""The zone, the records in it, and the site a record routes to.

Two modules, one per aggregate, named for what they hold rather than for the
layer they sit in. `zone.py` is a delegated domain; `record.py` is one answer
inside one — an address of its own, or the name of the site that answers for
its hostname — together with the partial update that changes one.

They have different lifetimes and different invariants, which is the whole
reason they are separate: a zone is delegated once and validated as a name,
while a record is created, repointed and deleted against the zone that holds
it. The package is the capability's public face, so `dns.domain` still means
what it did when it was a file.
"""

from blitzecdn.capabilities.dns.domain.record import (
    DnsRecord,
    RecordPatch,
    RecordType,
)
from blitzecdn.capabilities.dns.domain.zone import Domain

__all__ = ["DnsRecord", "Domain", "RecordPatch", "RecordType"]
