"""The Redis broker adapter: process wiring and queue publication.

This is the publishing half of the queue. :mod:`blitzecdn.worker` is the
consuming half — the process the ``dramatiq`` CLI runs, and the only module
that defines actors. Keeping the two apart is what lets the composition root
depend on the queue without depending on the worker: ``control_plane`` imports
this module, ``worker`` imports ``control_plane``, and the arrow never turns
back on itself.

Publication is therefore by *name* rather than through an actor object. A
publisher — the API process, the CLI, the scheduler — never imports the actor
it is enqueueing for, and does not need to: a Dramatiq message carries the
actor name and the queue, and the worker process resolves both against the
actors it declared. The retry policy an actor is decorated with is read by the
consumer, so it stays exactly where it is written.

The names below are the wire contract between the two halves.
``tests/test_queue.py`` holds them against the actors in
:mod:`blitzecdn.worker`, so a queue renamed on one side cannot silently start
publishing into a queue nothing consumes.
"""

from __future__ import annotations

from uuid import uuid4

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from redis import Redis

#: The queue each kind of durable work is published to. These are the
#: ``queue_name`` arguments on the actors in :mod:`blitzecdn.worker`.
DEPLOYMENT_QUEUE = "deployments"
SCHEDULED_QUEUE = "scheduled"

#: The actor that converges one already-recorded queued deployment.
RUN_DEPLOYMENT_ACTOR = "run_deployment"

_SCHEDULE_KEY_PREFIX = "blitzecdn:scheduled:"
_REDIS_OPERATION_TIMEOUT_SECONDS = 5.0
_RELEASE_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_broker_url: str | None = None


def scheduled_actor_name(operation: str) -> str:
    """The actor that runs one maintenance operation.

    Operations are spelled with hyphens because they are a
    :class:`~blitzecdn.domain.operations.MaintenanceOperation` value; actors are
    Python functions. This is the whole of the translation between them.
    """
    return operation.replace("-", "_")


def configure_broker(redis_url: str) -> None:
    """Install one Redis broker per process."""
    global _broker_url
    if _broker_url == redis_url:
        return
    dramatiq.set_broker(RedisBroker(url=redis_url))  # type: ignore[no-untyped-call]
    _broker_url = redis_url


def publish(actor_name: str, queue_name: str, *args: object) -> None:
    """Enqueue one message for an actor this process need not have imported.

    The queue is declared first because a broker rejects a message for a queue
    it has not seen, and a publisher that never imports the actors has never
    declared one. Declaring is idempotent on every broker.
    """
    broker = dramatiq.get_broker()
    broker.declare_queue(queue_name)
    broker.enqueue(
        dramatiq.Message(
            queue_name=queue_name,
            actor_name=actor_name,
            args=args,
            kwargs={},
            options={},
        )
    )


class DramatiqBackgroundRunner:
    """Queues deployment identifiers for the worker entry point.

    Satisfies :class:`~blitzecdn.application.ports.deployments.QueueBackgroundRunner`
    structurally; the port is not imported here so the adapter keeps facing
    outward only.
    """

    def __init__(self, redis_url: str) -> None:
        configure_broker(redis_url)

    def enqueue(self, deployment_id: str) -> None:
        publish(RUN_DEPLOYMENT_ACTOR, DEPLOYMENT_QUEUE, deployment_id)


def redis_ready(redis_url: str) -> bool:
    """Whether the mandatory broker answers within the readiness budget."""
    client = Redis.from_url(redis_url, socket_connect_timeout=1.0, socket_timeout=1.0)
    try:
        return bool(client.ping())
    finally:
        client.close()


def _client(redis_url: str) -> Redis:
    return Redis.from_url(
        redis_url,
        socket_connect_timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
        socket_timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
    )


def enqueue_scheduled_once(redis_url: str, operation: str, *, ttl_seconds: int) -> bool:
    """Atomically publish at most one queued/running copy of an operation."""
    client = _client(redis_url)
    key = f"{_SCHEDULE_KEY_PREFIX}{operation}"
    token = uuid4().hex
    try:
        if not client.set(key, token, nx=True, ex=ttl_seconds):
            return False
        try:
            publish(scheduled_actor_name(operation), SCHEDULED_QUEUE, token)
        except BaseException:
            client.eval(_RELEASE_IF_OWNER, 1, key, token)
            raise
        return True
    finally:
        client.close()


def release_schedule_key(redis_url: str, operation: str, token: str) -> None:
    """Release the single-flight key, but only if this run still owns it."""
    client = _client(redis_url)
    try:
        client.eval(
            _RELEASE_IF_OWNER,
            1,
            f"{_SCHEDULE_KEY_PREFIX}{operation}",
            token,
        )
    finally:
        client.close()
