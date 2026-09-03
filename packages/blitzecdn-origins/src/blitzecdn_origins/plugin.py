"""Register the fleet origin-check capability from an installable distribution.

Two routers, one command group, the role and the play this distribution ships,
and the name to attribute them to. Everything the capability actually *does* is
in ``service.py``, reached by an explicit call on a service this package builds
for itself in ``composition.py`` — nothing about probing an origin goes through
a hook.

The Ansible contribution names *no* role for either of the edge play's slots.
That is the shape of an operation rather than a convergence: the role is
reached only by this package's own play, on demand, so core adds the directory
to Ansible's search path and the edge play runs nothing extra. A deploy
converges byte-identically whether or not this package is attached.

Nothing in ``blitzecdn`` imports this module. The ``blitzecdn.plugins`` entry
point in this distribution's metadata is the whole of how it is found, so
installing the package makes the route and the command appear and removing it
makes them go away, with no line of core edited either way.
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter

from blitzecdn.core.plugins import (
    AnsibleContribution,
    CliCommandGroup,
    PluginMetadata,
    hookimpl,
)
from blitzecdn_origins import ansible, cli
from blitzecdn_origins.api import routes
from blitzecdn_origins.composition import __version__


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="origins",
        version=__version__,
        required=False,
        provides=frozenset({"origins"}),
        summary="Probe every site's origin from the edges that proxy to it.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (routes.router,)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    # `origin check`, exactly as it read before this became a package. The
    # command tree is an interface decision and not a consequence of
    # packaging.
    return (CliCommandGroup(name="origin", app=cli.origin_app),)


@hookimpl
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    # The probe role, from inside this wheel, and neither slot of the edge
    # play: this role is reached by this package's own play and converges
    # nothing on a deploy. Core adds the directory to Ansible's role search
    # path and never learns what is in it.
    return (AnsibleContribution(plugin="origins", roles_path=ansible.ROLES_PATH),)
