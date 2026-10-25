"""`domain`, `record`, `rule` and `dns` — the zone editor's command groups.

dns founds the `domain` group but ships only the commands about the zone
itself and the visitor headers serving owns. A command that edits another
capability's contract — `domain cache`, `domain ssl`, `domain firewall` — is
contributed by that capability and joins the group at registration, so an
exemption written for one contract's commands covers those and no others.

`DOMAIN_ORDER` below is the reading order of the whole group, contributed
commands included. A command missing from it refuses to start the command
line rather than being sorted quietly to the end, because a command that works
but that nobody reading `--help` finds where it belongs is the failure it
prevents.
"""

from blitzecdn.capabilities.dns.cli import (  # noqa: F401 - registration
    headers,
    record,
    rule,
    zone,
)
from blitzecdn.capabilities.dns.cli.app import (
    dns_app,
    domain_app,
    record_app,
    rule_app,
)

__all__ = ["DOMAIN_ORDER", "dns_app", "domain_app", "record_app", "rule_app"]

#: The zone itself first, then the contracts, roughly outermost to innermost:
#: how it is reached, how it is secured, what it caches, what it sends on.
DOMAIN_ORDER = (
    "add",
    "list",
    "hosts",
    "show",
    "origin",
    "enable",
    "remove",
    "ssl",
    "ssl-automatic",
    "minimum-tls",
    "http3",
    "max-upload-size",
    "always-use-https",
    "cache",
    "cache-query-string",
    "under-attack",
    "compression",
    "visitor-headers",
    "firewall",
)
