"""Dramatiq actors for durable, out-of-process control-plane work."""

from __future__ import annotations

import logging
from uuid import uuid4

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from redis import Redis

from blitzecdn.config import Settings
from blitzecdn.domain.operations import MaintenanceOperation
from blitzecdn.exceptions import DeploymentBusyError

_LOGGER = logging.getLogger(__name__)
_broker_url: str | None = None
_SCHEDULE_KEY_PREFIX = "blitzecdn:scheduled:"
_REDIS_OPERATION_TIMEOUT_SECONDS = 5.0
_DEPLOYMENT_LOCK_RETRIES = 260
_RELEASE_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class DramatiqBackgroundRunner:
    """Queues deployment identifiers for this worker entry point."""

    def __init__(self, redis_url: str) -> None:
        configure_broker(redis_url)

    def enqueue(self, deployment_id: str) -> None:
        run_deployment.send(deployment_id)


def redis_ready(redis_url: str) -> bool:
    """Whether the mandatory broker answers within the readiness budget."""
    client = Redis.from_url(redis_url, socket_connect_timeout=1.0, socket_timeout=1.0)
    try:
        return bool(client.ping())
    finally:
        client.close()


def enqueue_scheduled_once(redis_url: str, operation: str, *, ttl_seconds: int) -> bool:
    """Atomically publish at most one queued/running copy of an operation."""
    client = Redis.from_url(
        redis_url,
        socket_connect_timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
        socket_timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
    )
    key = f"{_SCHEDULE_KEY_PREFIX}{operation}"
    token = uuid4().hex
    try:
        if not client.set(key, token, nx=True, ex=ttl_seconds):
            return False
        try:
            globals()[operation.replace("-", "_")].send(token)
        except BaseException:
            client.eval(_RELEASE_IF_OWNER, 1, key, token)
            raise
        return True
    finally:
        client.close()


def _release_schedule_key(operation: str, token: str) -> None:
    settings = Settings.from_environment()
    client = Redis.from_url(
        str(settings.redis_url),
        socket_connect_timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
        socket_timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
    )
    try:
        client.eval(
            _RELEASE_IF_OWNER,
            1,
            f"{_SCHEDULE_KEY_PREFIX}{operation}",
            token,
        )
    finally:
        client.close()


def configure_broker(redis_url: str) -> None:
    """Install one Redis broker per process."""
    global _broker_url
    if _broker_url == redis_url:
        return
    broker = RedisBroker(url=redis_url)  # type: ignore[no-untyped-call]
    dramatiq.set_broker(broker)
    # An actor captures the current broker when decorated. The API may supply
    # Settings from an injected mapping after this module was imported, so
    # rebind an existing actor as well as Dramatiq's process default.
    for name in (
        "run_deployment",
        "reconcile_certificates",
        "reconcile_automatic_ssl",
        "renew_certificates",
        "check_drift",
    ):
        actor = globals().get(name)
        if actor is not None:
            actor.broker = broker
    _broker_url = redis_url


# The worker imports this module before accepting messages. Resolve through the
# same settings pipeline as the API so a Redis URL in blitzecdn.toml is honored;
# environment overrides still win through Settings.from_environment().
configure_broker(str(Settings.from_environment().redis_url))


def _retry_locked_deployment(retries: int, exception: BaseException) -> bool:
    """Retry only the publish-to-worker lock handoff race."""
    return retries < _DEPLOYMENT_LOCK_RETRIES and isinstance(
        exception, DeploymentBusyError
    )


@dramatiq.actor(
    queue_name="deployments",
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

    control = build_control_plane(Settings.from_environment())
    try:
        control.maintenance.run(MaintenanceOperation(operation))
    finally:
        try:
            control.close()
        finally:
            _release_schedule_key(operation, token)


@dramatiq.actor(max_retries=0, queue_name="scheduled")
def reconcile_certificates(token: str) -> None:
    _run_control_plane("reconcile-certificates", token)


@dramatiq.actor(max_retries=0, queue_name="scheduled")
def reconcile_automatic_ssl(token: str) -> None:
    _run_control_plane("reconcile-automatic-ssl", token)


@dramatiq.actor(max_retries=0, queue_name="scheduled")
def renew_certificates(token: str) -> None:
    _run_control_plane("renew-certificates", token)


@dramatiq.actor(max_retries=0, queue_name="scheduled")
def check_drift(token: str) -> None:
    _run_control_plane("check-drift", token)
