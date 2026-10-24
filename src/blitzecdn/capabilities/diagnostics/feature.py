"""Reporting on the rest of the control plane, and answering for its health.

The health checks themselves are operations, built in
:mod:`blitzecdn.capabilities.diagnostics.runtime`.
"""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.diagnostics import cli
from blitzecdn.capabilities.diagnostics.api import readiness, routes
from blitzecdn.core.plugins import CliCommandGroup, Feature, PluginMetadata

FEATURES = (
    Feature(
        metadata=PluginMetadata(
            name="diagnostics",
            version=__version__,
            api_version=2,
            required=True,
            summary="Health, metrics, the audit trail, and the API server itself.",
        ),
        api_routers=(readiness.router, routes.router),
        # `ansible` is a named group rather than more root verbs because
        # neither of its commands is an operator's verb: both answer "what did
        # the installed capabilities compose into this process-wide Ansible
        # value", which a caller driving ansible-playbook itself asks.
        cli_commands=(
            CliCommandGroup(plugin="diagnostics", name=None, app=cli.diagnostics_app),
            CliCommandGroup(plugin="diagnostics", name="ansible", app=cli.ansible_app),
        ),
    ),
)
