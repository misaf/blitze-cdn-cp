"""What the zone editor decides.

`zones.py` is the whole of it: creating and deleting a zone, the records in
one, routing a hostname to a site and taking it off again, and the hostname
projection that keeps `sites` in step. One module because it is one decision
maker — a record cannot be routed without the zone it lives in agreeing, and
splitting the two would put that agreement across a module boundary.

A directory rather than a file because a slice's layers are directories here,
whether or not this one has grown a second module yet. The name inside says
what it holds; the directory says which rule it lives under.
"""

from blitzecdn.capabilities.dns.service.zones import DnsService

__all__ = ["DnsService"]
