"""The worker entry point: the Dramatiq actors, and nothing else.

This module is what ``dramatiq blitzecdn.worker`` imports. It is an entry point
in exactly the sense the CLI and the API are: it builds a control plane and
calls a service on it, and it is allowed to know both halves because nothing
imports it back.

There are two actors and there will not be more. One converges a queued
deployment; the other runs a scheduled job *by name*, resolving that name
against the plugin registry in this process. That indirection is what lets a
separately installed plugin contribute recurring work: ``blitzecdn-waf`` can
add a "refresh rule set" job without an actor being declared for it here, which
is the whole point of the registration mechanism.

The broker itself — connecting to Redis, publishing a message, the
single-flight key that keeps one scheduled job in flight — lives in
:mod:`blitzecdn.core.broker`, which is where the composition root reaches for
it. A publisher never imports this module.
"""

from __future__ import annotations

import logging

import dramatiq

from blitzecdn.core.broker import (
    DEPLOYMENT_QUEUE,
    SCHEDULED_QUEUE,
    configure_broker,
    release_schedule_key,
)
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import DeploymentBusyError
from blitzecdn.core.plugins import ProcessKind

_LOGGER = logging.getLogger(__name__)
_DEPLOYMENT_LOCK_RETRIES = 260


# The worker imports this module before accepting messages. Resolve through the
# same settings pipeline as the API so a Redis URL in blitzecdn.toml is honored;
# environment overrides still win through Settings.from_environment(). Actors
# capture the current broker when decorated, so this has to run first.
configure_broker(str(Settings.from_environment().redis_url))


def _retry_locked_deployment(retries: int, exception: BaseException) -> bool:
    """Retry only the publish-to-worker lock handoff race."""
    return retries < _DEPLOYMENT_LOCK_RETRIES and isinstance(
        exception, DeploymentBusyError
    )


@dramatiq.actor(
    queue_name=DEPLOYMENT_QUEUE,
    retry_when=_retry_locked_deployment,
    min_backoff=1_000,
    max_backoff=30_000,
)
def run_deployment(deployment_id: str) -> None:
    """Converge one already-recorded queued deployment."""
    from blitzecdn.bootstrap import build_control_plane

    settings = Settings.from_environment()
    control = build_control_plane(settings, process=ProcessKind.WORKER)
    try:
        control.deployments.run_queued(deployment_id)
    except Exception:
        _LOGGER.exception("deployment %s failed", deployment_id)
        raise
    finally:
        control.close()


@dramatiq.actor(max_retries=0, queue_name=SCHEDULED_QUEUE)
def run_scheduled_job(job: str, token: str) -> None:
    """Run one plugin-contributed scheduled job in the worker process."""
    from blitzecdn.bootstrap import build_control_plane

    settings = Settings.from_environment()
    control = build_control_plane(settings, process=ProcessKind.WORKER)
    try:
        control.maintenance.run(job)
    finally:
        try:
            control.close()
        finally:
            release_schedule_key(str(settings.redis_url), job, token)
