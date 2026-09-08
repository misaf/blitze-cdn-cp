"""What the zone editor decides, and what the rule editor decides beside it.

`zones.py` creates and deletes a zone, sets the policy its hostnames are served
by, owns the records in one, routes a hostname to a site and takes it off
again, and keeps the hostname projection `sites` reads in step. `rules.py` owns
the overrides on that policy, and the resolver that says what a hostname ends
up with.

Two modules rather than one because they are two decision makers over one
aggregate: every write in `zones.py` is about a zone or a record, and every
write in `rules.py` is about an exception to one. What they share is that a
rule cannot exist without its zone, and that is a read — which is why `rules.py`
holds a reader for it and not the editor.
"""

from blitzecdn.capabilities.dns.service.rules import RuleService
from blitzecdn.capabilities.dns.service.zones import DnsService

__all__ = ["DnsService", "RuleService"]
