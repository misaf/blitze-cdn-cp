"""`site` — the virtual hosts, and every setting that decides how one is served.

One module per contract whose fields its commands edit: `site.py` for what this
capability owns, and `tls`, `http`, `cache`, `compression`, `security` and
`headers` for the contracts composed into `SitePolicy`. Adding a switch to a
capability therefore has one obvious place to add its command, and the group no
longer grows by appending to whichever section a 572-line file happened to end
with.

This group used to hold two read-only commands, because a site was derived from
a proxied DNS record and everything that changed one was a `record` command.
Sites are canonical now, so the ten policy commands moved here from `dns` and
the create/delete pair that never existed came with them.

What is still not here is `server_names`: the hostnames a site answers on are
the records routed to it, so they are added and removed with
\'blitzecdn record route\' and \'blitzecdn record unroute\'.

The imports below are the registration — each module decorates `site_app`, so
importing this package is what makes the commands exist.
"""

from blitzecdn.capabilities.sites.cli import (  # noqa: F401
    cache,
    compression,
    headers,
    http,
    security,
    site,
    tls,
)
from blitzecdn.capabilities.sites.cli.app import site_app

#: The order `blitzecdn site --help` lists the commands in.
#:
#: Declared rather than inherited from import order, which is what a single
#: file gave us for free and a package does not: the imports above are sorted
#: alphabetically by the formatter, so the group would open with
#: `cache-query-string` and bury `create` in the middle. The reading order is a
#: property of the group, not of how its modules happen to be named.
_HELP_ORDER = (
    "create",
    "list",
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
    "cache-query-string",
    "under-attack",
    "compression",
    "visitor-headers",
    "firewall",
)


def _order_commands() -> None:
    """Put the group in reading order, and refuse to import if one is missing.

    A command left out of `_HELP_ORDER` has no place to go, and sorting it to
    the end silently would make this list decorative — the failure it exists to
    prevent is a command that works but that nobody reading `--help` finds in
    the group it belongs to. So an unlisted command is an import error naming
    itself, which is the same bargain `_assert_patch_covers_policy` makes for a
    site setting that cannot be patched.
    """
    registered = {
        # `name` is optional to typer — a command that omits it is published
        # under its function name — so an unnamed one has to be reported as
        # something rather than skipped past.
        command.name or getattr(command.callback, "__name__", "<unnamed>"): command
        for command in site_app.registered_commands
    }
    unlisted = sorted(name for name in registered if name not in _HELP_ORDER)
    if unlisted:
        raise RuntimeError(
            "every `site` command must be placed in _HELP_ORDER; these are not: "
            + ", ".join(unlisted)
        )
    site_app.registered_commands = [
        registered[name] for name in _HELP_ORDER if name in registered
    ]


_order_commands()

__all__ = ["site_app"]
