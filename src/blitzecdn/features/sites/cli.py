"""`site` — read the virtual hosts the edges serve.

Two commands, both read-only, and that is the whole shape of the capability
from the outside: a site is derived from a proxied record, so everything that
*changes* one is a `record` command over in `dns`.
"""

from __future__ import annotations

from typing import Annotated

import typer

from blitzecdn.cli import common

site_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect CDN virtual hosts (derived from proxied DNS records).",
)


@site_app.command("list")
def site_list(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List the virtual hosts the edges will serve.

    Derived, not created: only proxied records appear. A record you proxied but
    have not deployed is already listed here — this is desired state, not what
    the fleet is running. Use 'drift' for that.
    """
    common.emit(common.control_plane().sites.list_sites(), json_output=json_output)


@site_app.command("show")
def site_show(
    name: Annotated[str, typer.Argument(help="Site name, e.g. cdn-example-com.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the fully resolved policy for one site.

    Sites are derived from proxied DNS records rather than created, so this is
    where the defaults a record never mentions become visible — it is the same
    document the edges are handed.
    """
    common.emit(common.control_plane().sites.get_site(name), json_output=json_output)
