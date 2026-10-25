"""Reporting on the control plane, and answering for its health."""

from __future__ import annotations

from blitzecdn import __version__
from blitzecdn.capabilities.diagnostics import cli
from blitzecdn.capabilities.diagnostics.api import readiness, routes
from blitzecdn.core.features import CliCommandGroup, Feature, FeatureMetadata

FEATURES = (
    Feature(
        metadata=FeatureMetadata(
            name="diagnostics",
            version=__version__,
            summary="Health, metrics, the audit trail, and the API server itself.",
        ),
        api_routers=(readiness.router, routes.router),
        # `ansible` is a named group: its commands answer composition questions for
        # callers driving ansible-playbook, not operator verbs.
        cli_commands=(
            CliCommandGroup(feature="diagnostics", name=None, app=cli.diagnostics_app),
            CliCommandGroup(feature="diagnostics", name="ansible", app=cli.ansible_app),
        ),
    ),
)
