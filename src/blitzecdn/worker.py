"""The worker entry point: the Dramatiq actors, and nothing else.

This module is what ``dramatiq blitzecdn.worker`` imports. It is an entry
point in exactly the sense the CLI and the API are: it builds a control plane
and calls a service on it, and it is allowed to know both halves because
nothing imports it back.

The broker itself — connecting to Redis, publishing a message, the
single-flight key that keeps one scheduled operation in flight — lives in
:mod:`blitzecdn.infrastructure.broker`, which is where the composition root
reaches for it. A publisher never imports this module, so a Dramatiq message
carries an actor *name* and the worker resolves it against the actors declared
below.
"""

from __future__ import annotations

import logging

import dramatiq

from blitzecdn.config import Settings
from blitzecdn.domain.operations import MaintenanceOperation
from blitzecdn.exceptions import DeploymentBusyError
from blitzecdn.infrastructure.broker import (
    DEPLOYMENT_QUEUE,
    SCHEDULED_QUEUE,
    configure_broker,
    release_schedule_key,
)

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
    from blitzecdn.control_plane import build_control_plane

    settings = Settings.from_environment()
    control = build_control_plane(settings)
    try:
        control.deployments.run_queued(deployment_id)
    except Exception:
        _LOGGER.exception("deployment %s failed", deployment_id)
        raise
    finally:
        control.close()


def _run_control_plane(operation: str, token: str) -> None:
    """Run one scheduled operation in the worker process."""
    from blitzecdn.control_plane import build_control_plane

    settings = Settings.from_environment()
    control = build_control_plane(settings)
    try:
        control.maintenance.run(MaintenanceOperation(operation))
    finally:
        try:
            control.close()
        finally:
            release_schedule_key(str(settings.redis_url), operation, token)


@dramatiq.actor(max_retries=0, queue_name=SCHEDULED_QUEUE)
def reconcile_certificates(token: str) -> None:
    _run_control_plane(MaintenanceOperation.RECONCILE_CERTIFICATES, token)


@dramatiq.actor(max_retries=0, queue_name=SCHEDULED_QUEUE)
def reconcile_automatic_ssl(token: str) -> None:
    _run_control_plane(MaintenanceOperation.RECONCILE_AUTOMATIC_SSL, token)


@dramatiq.actor(max_retries=0, queue_name=SCHEDULED_QUEUE)
def renew_certificates(token: str) -> None:
    _run_control_plane(MaintenanceOperation.RENEW_CERTIFICATES, token)


@dramatiq.actor(max_retries=0, queue_name=SCHEDULED_QUEUE)
def check_drift(token: str) -> None:
    _run_control_plane(MaintenanceOperation.CHECK_DRIFT, token)
