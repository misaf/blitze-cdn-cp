"""The hook specifications: every extension point BlitzeCDN has.

There are twelve, and the list is meant to stay that short. A hook earns its place
by being a *registration* point — somewhere the core has to be told that
something exists — and nothing else. Business communication is not here and
must not come here: a caller that wants a certificate issued calls
``certificate_service.issue(...)``, because a hook call answers "whoever is
listening" and issuing a certificate is a thing exactly one service does.

The test for whether something belongs here: could a package that this
repository has never heard of implement it, and would core have to be edited if
one did? `blitzecdn-waf` contributes a router, a CLI group, a health check and a
slice of desired state, and none of those requires a line of core to change.
`issue_certificate` fails the test in both directions.

One rule covers the arguments: a hook whose contribution is static — a router,
a command group — takes nothing, and every other hook takes ``platform``, the
composition root with its services already built. A scheduled job needs the
service it will call; a health check needs the thing it will probe; a
certificate's desired state needs the store that knows where the key is on this
controller. That is registration-time access to a typed object, and it is the
only reason a plugin ever sees it. Reaching for a service *inside* a request is
the service-locator shape this deliberately does not offer, and
``tests/architecture/test_layering.py`` refuses ``platform`` anywhere but ``plugin.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import pluggy

from blitzecdn.core.plugins.types import (
    PROJECT_NAME,
    AnsibleContribution,
    CliCommandGroup,
    ConfigurationContribution,
    FleetStateContribution,
    HealthCheck,
    NginxContribution,
    PluginMetadata,
    RuntimeContext,
    ScheduledJob,
    SiteStateContribution,
    ValidationIssue,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from fastapi import APIRouter

    from blitzecdn.capabilities.sites.domain import CdnSite
    from blitzecdn.composition import ControlPlane

hookspec = pluggy.HookspecMarker(PROJECT_NAME)

#: Applied by every implementation, in this package and outside it. Exported
#: from `blitzecdn.core.plugins` so a plugin's only import from the control
#: plane can be the one line that marks its functions.
hookimpl = pluggy.HookimplMarker(PROJECT_NAME)


@hookspec(firstresult=True)
def blitzecdn_plugin_metadata() -> PluginMetadata | None:
    """Identify this plugin.

    Required of every plugin: the name is how a duplicate is detected, how a
    failure is attributed, and how ``blitzecdn plugins`` lists what is
    installed. A plugin that does not answer is refused at registration rather
    than allowed to contribute anonymously.
    """


@hookspec
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    """Contribute HTTP routes.

    Takes nothing: a router reaches the control plane through FastAPI's own
    dependency injection, which is already the mechanism for "this request
    needs that service" and does not need a second one beside it.
    """


@hookspec
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    """Contribute commands to the ``blitzecdn`` command line.

    Also takes nothing, and for a reason worth stating: the command tree has to
    exist before an argument is parsed, and building services to discover the
    tree would mean ``blitzecdn --help`` created and migrated the database.
    Commands resolve the control plane when they run, not when they register.
    """


@hookspec
def blitzecdn_ansible_contributions() -> Sequence[AnsibleContribution]:
    """Contribute the Ansible roles this plugin ships inside its own wheel.

    Takes nothing, for the same reason a router does: the role search path is
    composed once, before any adapter exists, and a package answering this is
    reporting where its own files landed rather than asking anything of the
    control plane.

    The path must be a real directory on this filesystem — Ansible resolves
    roles by opening them — which is what ``importlib.resources`` gives back
    for a wheel-installed package. Core adds the directory to the search path,
    refuses two packages that ship the same role name, and knows nothing else
    about what is in there.
    """


@hookspec
def blitzecdn_capability_configuration() -> Sequence[ConfigurationContribution]:
    """Claim the `BLITZE_*` names this capability asks an operator to set.

    Takes nothing, like the other static contributions: a package answering
    this is naming what it owns, not asking anything of a control plane that
    does not exist yet. Configuration has to be resolved before the first
    adapter is built — it is what several of them are built *from* — so this
    is among the earliest things core asks.

    Both halves of the claim are here, secrets and settings alike, because an
    operator asking "what does this capability need configured" is asking one
    question. See :class:`ConfigurationContribution`.

    Claiming is the whole mechanism. Core stages every non-core `BLITZE_*`
    name it can see and matches it against these claims; an unclaimed name is
    refused by the name that was set, and a claimed one is resolved, checked
    and handed back to its owner alone. A capability that needs no
    configuration does not implement this hook.
    """


@hookspec
def blitzecdn_nginx_contributions() -> Sequence[NginxContribution]:
    """Contribute package-owned static fragments at stable Nginx contexts."""


@hookspec
def blitzecdn_health_checks(platform: ControlPlane) -> Sequence[HealthCheck]:
    """Contribute a reason ``/health`` may report the node as unavailable."""


@hookspec
def blitzecdn_scheduled_jobs(platform: ControlPlane) -> Sequence[ScheduledJob]:
    """Contribute recurring maintenance, with its interval and its work.

    The job carries its own callable. The scheduler process only publishes the
    name and the worker resolves it against this same registry in its own
    process, so a job is one object rather than an interval here and a matching
    actor somewhere else that has to be kept in step by hand.
    """


@hookspec
def blitzecdn_site_desired_state(
    site: CdnSite, platform: ControlPlane
) -> SiteStateContribution | None:
    """Contribute this plugin's share of one virtual host's Ansible document.

    Return ``None`` — the default for a plugin that does not implement this —
    rather than an empty contribution, so a plugin that has nothing to say
    about a site costs nothing to merge.
    """


@hookspec
def blitzecdn_fleet_desired_state(
    sites: tuple[CdnSite, ...], platform: ControlPlane
) -> FleetStateContribution | None:
    """Contribute variables derived from the whole fleet rather than one site."""


@hookspec
def blitzecdn_deployment_checks(
    site: CdnSite, platform: ControlPlane
) -> Sequence[ValidationIssue]:
    """Report what this plugin knows that should stop or qualify a deployment.

    Runs before desired state is rendered, so a blocking issue costs nothing —
    no file is written and no playbook starts.
    """


@hookspec
def blitzecdn_startup(context: RuntimeContext, platform: ControlPlane) -> None:
    """Do the work this plugin owes the process that is starting.

    ``context.process`` says which one, because the answer differs: only the
    API republishes queued deployments and marks orphaned runs abandoned, and
    doing it from every CLI invocation would take the fleet-wide deployment
    lock on every command an operator typed.
    """


@hookspec
def blitzecdn_shutdown(context: RuntimeContext, platform: ControlPlane) -> None:
    """Release what ``blitzecdn_startup`` acquired, in reverse order."""


__all__ = ["hookimpl", "hookspec"]
