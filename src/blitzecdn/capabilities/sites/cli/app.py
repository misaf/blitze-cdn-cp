"""The `site` group itself, and the two helpers every command in it uses.

Separate from the commands so that a command module can import the group
without importing its siblings. Nothing here is a command; `__init__` imports
the modules that are, and each of those registers against this `site_app`.
"""

from __future__ import annotations

import typer

from blitzecdn.capabilities.sites.domain import CdnSite, SitePatch
from blitzecdn.cli import common

site_app = typer.Typer(
    no_args_is_help=True,
    help="Manage CDN virtual hosts and the policy each one is served with.",
)

__all__ = ["site_app"]


def _update(name: str, patch: SitePatch) -> CdnSite:
    return common.control_plane().site_editor.update_site(name, patch, "cli")


def _applied(site: CdnSite, message: str) -> str:
    """A confirmation that says whether anything is actually serving yet."""
    if site.server_names:
        return f"{message} Run 'blitzecdn deploy' to apply."
    return (
        f"{message} No hostname routes to {site.name!r} yet, so nothing is "
        "served — use 'blitzecdn record route' to point one here."
    )
