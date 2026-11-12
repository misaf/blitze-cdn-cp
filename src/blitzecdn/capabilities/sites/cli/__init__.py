"""`domain`, `record` and `rule` — the zone editor's command groups.

sites founds `domain` but ships only the zone commands; a
command editing another capability's contract is contributed by it.
`DOMAIN_ORDER` is the whole group's `--help` order, and a command missing from
it stops the CLI from starting.
"""

from blitzecdn.capabilities.sites.cli import (  # noqa: F401 - registration
    record,
    rule,
    zone,
)
from blitzecdn.capabilities.sites.cli.app import (
    domain_app,
    record_app,
    rule_app,
)

__all__ = ["DOMAIN_ORDER", "domain_app", "record_app", "rule_app"]

#: The zone itself first, then the contracts, roughly outermost to innermost:
#: how it is reached, how it is secured, what it caches, what it sends on.
DOMAIN_ORDER = (
    "add",
    "list",
    "hosts",
    "export",
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
    "rate-limit",
    "error-pages",
    "maintenance",
)
