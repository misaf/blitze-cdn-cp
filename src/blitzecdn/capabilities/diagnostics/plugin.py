"""Reporting on the rest of the control plane, and answering for its health.

The two health checks are the ones that decide whether this node can do work at
all: it must be able to read its own database, and it must be able to reach the
queue, because a controller that can accept a deployment and never run it is
worse than one that refuses it. A capability with its own liveness question
contributes its own check here rather than editing `/health`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from fastapi import APIRouter

from blitzecdn import __version__
from blitzecdn.capabilities.diagnostics import cli
from blitzecdn.capabilities.diagnostics.api import readiness, routes
from blitzecdn.core.plugins import (
    CliCommandGroup,
    HealthCheck,
    PluginMetadata,
    hookimpl,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from blitzecdn.composition import ControlPlane


@hookimpl
def blitzecdn_plugin_metadata() -> PluginMetadata:
    return PluginMetadata(
        name="diagnostics",
        version=__version__,
        required=True,
        summary="Health, metrics, the audit trail, and the API server itself.",
    )


@hookimpl
def blitzecdn_api_routers() -> Sequence[APIRouter]:
    return (readiness.router, routes.router)


@hookimpl
def blitzecdn_cli_commands() -> Sequence[CliCommandGroup]:
    # `ansible` is a named group rather than more root verbs because neither of
    # its commands is an operator's verb: both answer "what did the installed
    # capabilities compose into this process-wide Ansible value", which is a
    # question a *caller driving ansible-playbook itself* asks — the justfile,
    # a lint step, an air-gapped controller running a play by hand.
    return (
        CliCommandGroup(name=None, app=cli.diagnostics_app),
        CliCommandGroup(name="ansible", app=cli.ansible_app),
    )


@hookimpl
def blitzecdn_health_checks(platform: ControlPlane) -> Sequence[HealthCheck]:
    def database() -> None:
        platform.workflow_history.list_workflows(1)

    def broker() -> None:
        if not platform.broker_ready():
            raise ConnectionError("Redis did not answer PING")

    return (
        HealthCheck(name="database", check=database),
        HealthCheck(name="broker", check=broker),
    )
