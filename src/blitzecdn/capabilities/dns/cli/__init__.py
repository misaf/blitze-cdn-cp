"""`domain`, `record`, `rule` and `dns` — the zone editor's command groups.

A directory rather than a file because the `domain` group is nineteen commands
across seven capabilities' contracts. It was one 572-line module once, under
`sites`, and every unrelated command in it shared the firewall exemption
written for two of them; splitting by the contract each command edits is what
made those exemptions specific again.

`_HELP_ORDER` below is the reading order of the `domain` group, and a command
missing from it is an import error rather than a command sorted quietly to the
end. The failure it prevents is a command that works but that nobody reading
`--help` finds in the group it belongs to.
"""

from blitzecdn.capabilities.dns.cli import (  # noqa: F401 - registration
    cache,
    compression,
    headers,
    http,
    record,
    rule,
    security,
    tls,
    zone,
)
from blitzecdn.capabilities.dns.cli.app import (
    dns_app,
    domain_app,
    record_app,
    rule_app,
)

__all__ = ["dns_app", "domain_app", "record_app", "rule_app"]

#: The zone itself first, then the contracts, roughly outermost to innermost:
#: how it is reached, how it is secured, what it caches, what it sends on.
_HELP_ORDER = (
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


def _order_commands() -> None:
    """Put the group in reading order, and refuse to import if one is missing."""
    registered = {
        # `name` is optional to typer — a command that omits it is published
        # under its function name — so an unnamed one has to be reported as
        # something rather than skipped past.
        command.name or getattr(command.callback, "__name__", "<unnamed>"): command
        for command in domain_app.registered_commands
    }
    unlisted = sorted(name for name in registered if name not in _HELP_ORDER)
    if unlisted:
        raise RuntimeError(
            "every `domain` command must be placed in _HELP_ORDER; these are "
            "not: " + ", ".join(unlisted)
        )
    domain_app.registered_commands = [
        registered[name] for name in _HELP_ORDER if name in registered
    ]


_order_commands()
