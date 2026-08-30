"""Dramatiq queue contracts without a live Redis server.

The queue has two halves that never import each other: ``infrastructure.broker``
publishes, ``worker`` consumes. The tests below cover each half on its own and
then hold the two together on the only thing they share — the queue and actor
names on the wire.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import dramatiq
import pytest
from dramatiq.brokers.stub import StubBroker

from blitzecdn import worker
from blitzecdn.application.maintenance import MaintenanceService
from blitzecdn.exceptions import DeploymentBusyError
from blitzecdn.infrastructure import broker as queue
from blitzecdn.worker import (
    check_drift,
    reconcile_automatic_ssl,
    reconcile_certificates,
    renew_certificates,
    run_deployment,
)


class FakeRedis:
    values: ClassVar[dict[str, str]] = {}

    @classmethod
    def from_url(cls, _url: str, **_kwargs):
        return cls()

    def set(self, key: str, value: str, **_kwargs) -> bool:
        if key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, _script: str, _keys: int, key: str, token: str) -> int:
        if self.values.get(key) != token:
            return 0
        del self.values[key]
        return 1

    def close(self) -> None:
        pass


def test_actors_publish_to_the_expected_queues():
    broker = StubBroker()
    dramatiq.set_broker(broker)
    actors = (
        run_deployment,
        reconcile_certificates,
        reconcile_automatic_ssl,
        renew_certificates,
        check_drift,
    )
    previous = [actor.broker for actor in actors]
    try:
        for actor in actors:
            actor.broker = broker
            broker.declare_actor(actor)
        run_deployment.send("deployment-id")
        reconcile_certificates.send("reconcile-token")
        reconcile_automatic_ssl.send("automatic-ssl-token")
        renew_certificates.send("renew-token")
        check_drift.send("drift-token")

        deployment = dramatiq.Message.decode(broker.queues["deployments"].get_nowait())
        scheduled = [
            dramatiq.Message.decode(broker.queues["scheduled"].get_nowait())
            for _ in range(4)
        ]
        assert deployment.args == ("deployment-id",)
        assert {message.actor_name for message in scheduled} == {
            "reconcile_certificates",
            "reconcile_automatic_ssl",
            "renew_certificates",
            "check_drift",
        }
        assert {message.args for message in scheduled} == {
            ("reconcile-token",),
            ("automatic-ssl-token",),
            ("renew-token",),
            ("drift-token",),
        }
    finally:
        for actor, original in zip(actors, previous, strict=True):
            actor.broker = original


def test_the_published_names_match_the_actors_that_consume_them():
    """The one thing the two halves have to agree on, asserted rather than assumed.

    The broker publishes by name so a web process never imports an actor. That
    trade buys process isolation and costs a compile-time check, and this is the
    check bought back: renaming a queue or an actor on one side without the
    other would publish into a queue nothing reads, which fails as a deployment
    that stays QUEUED forever rather than as an error.
    """
    assert run_deployment.actor_name == queue.RUN_DEPLOYMENT_ACTOR
    assert run_deployment.queue_name == queue.DEPLOYMENT_QUEUE
    scheduled = (
        reconcile_certificates,
        reconcile_automatic_ssl,
        renew_certificates,
        check_drift,
    )
    for actor in scheduled:
        assert actor.queue_name == queue.SCHEDULED_QUEUE
    assert {actor.actor_name for actor in scheduled} == {
        queue.scheduled_actor_name(operation)
        for operation in (
            "reconcile-certificates",
            "reconcile-automatic-ssl",
            "renew-certificates",
            "check-drift",
        )
    }


def test_the_broker_is_installed_once_per_process(monkeypatch):
    """Reconfiguring to the same URL must not replace a live broker.

    The API resolves its Redis URL through Settings, which may differ from the
    one this module bound at import. Rebinding is therefore allowed, but doing
    it on every control plane a process builds would drop connections under a
    running worker pool.
    """
    built: list[str] = []
    previous = dramatiq.get_broker()
    monkeypatch.setattr(queue, "_broker_url", None)
    monkeypatch.setattr(
        queue, "RedisBroker", lambda url: built.append(url) or StubBroker()
    )
    try:
        queue.configure_broker("redis://one")
        queue.configure_broker("redis://one")
        queue.configure_broker("redis://two")
    finally:
        dramatiq.set_broker(previous)

    assert built == ["redis://one", "redis://two"]


def test_readiness_pings_within_a_bounded_budget(monkeypatch):
    """The health endpoint must fail fast rather than hang on a dead broker."""
    seen: list[dict[str, float]] = []
    closed: list[bool] = []

    class PingingRedis(FakeRedis):
        @classmethod
        def from_url(cls, _url: str, **kwargs):
            seen.append(kwargs)
            return cls()

        def ping(self) -> bool:
            return True

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(queue, "Redis", PingingRedis)

    assert queue.redis_ready("redis://test") is True
    assert seen == [{"socket_connect_timeout": 1.0, "socket_timeout": 1.0}]
    assert closed == [True]


def test_publishing_declares_the_queue_it_publishes_to():
    """A publisher that never imported an actor has declared no queue."""
    broker = StubBroker()
    previous = dramatiq.get_broker()
    dramatiq.set_broker(broker)
    try:
        queue.publish("run_deployment", queue.DEPLOYMENT_QUEUE, "deployment-id")
        message = dramatiq.Message.decode(
            broker.queues[queue.DEPLOYMENT_QUEUE].get_nowait()
        )
    finally:
        dramatiq.set_broker(previous)

    assert message.actor_name == "run_deployment"
    assert message.args == ("deployment-id",)
    assert message.kwargs == {}


def test_the_background_runner_publishes_only_the_deployment_id(monkeypatch):
    """The composition root's queue adapter, exercised without Redis."""
    broker = StubBroker()
    previous = dramatiq.get_broker()
    dramatiq.set_broker(broker)
    monkeypatch.setattr(queue, "configure_broker", lambda _url: None)
    try:
        queue.DramatiqBackgroundRunner("redis://test").enqueue("deployment-id")
        message = dramatiq.Message.decode(
            broker.queues[queue.DEPLOYMENT_QUEUE].get_nowait()
        )
    finally:
        dramatiq.set_broker(previous)

    assert message.actor_name == queue.RUN_DEPLOYMENT_ACTOR
    assert message.args == ("deployment-id",)


def test_scheduled_enqueue_is_atomic_and_passes_ownership_token(monkeypatch):
    FakeRedis.values.clear()
    sent: list[tuple[str, str, tuple[object, ...]]] = []
    monkeypatch.setattr(queue, "Redis", FakeRedis)
    monkeypatch.setattr(
        queue,
        "publish",
        lambda actor, queue_name, *args: sent.append((actor, queue_name, args)),
    )

    assert queue.enqueue_scheduled_once("redis://test", "check-drift", ttl_seconds=60)
    assert not queue.enqueue_scheduled_once(
        "redis://test", "check-drift", ttl_seconds=60
    )
    assert len(sent) == 1
    actor_name, queue_name, args = sent[0]
    assert actor_name == "check_drift"
    assert queue_name == queue.SCHEDULED_QUEUE
    assert args == (FakeRedis.values[f"{queue._SCHEDULE_KEY_PREFIX}check-drift"],)


def test_failed_scheduled_publish_releases_its_key(monkeypatch):
    FakeRedis.values.clear()
    monkeypatch.setattr(queue, "Redis", FakeRedis)

    def fail(*_args: object) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(queue, "publish", fail)
    with pytest.raises(RuntimeError, match="broker unavailable"):
        queue.enqueue_scheduled_once("redis://test", "check-drift", ttl_seconds=60)

    assert FakeRedis.values == {}


def test_releasing_a_schedule_key_only_affects_the_run_that_owns_it(monkeypatch):
    FakeRedis.values.clear()
    monkeypatch.setattr(queue, "Redis", FakeRedis)
    key = f"{queue._SCHEDULE_KEY_PREFIX}check-drift"
    FakeRedis.values[key] = "owner-token"

    queue.release_schedule_key("redis://test", "check-drift", "someone-else")
    assert FakeRedis.values == {key: "owner-token"}

    queue.release_schedule_key("redis://test", "check-drift", "owner-token")
    assert FakeRedis.values == {}


def test_deployment_actor_retries_only_a_busy_fleet_lock():
    retry_when = run_deployment.options["retry_when"]

    assert retry_when(0, DeploymentBusyError("busy"))
    assert not retry_when(worker._DEPLOYMENT_LOCK_RETRIES, DeploymentBusyError("busy"))
    assert not retry_when(0, RuntimeError("broken deployment"))


def test_scheduled_redis_calls_have_finite_network_timeouts(monkeypatch):
    options: list[dict[str, float]] = []

    class RecordingRedis(FakeRedis):
        @classmethod
        def from_url(cls, _url: str, **kwargs):
            options.append(kwargs)
            return cls()

    RecordingRedis.values.clear()
    monkeypatch.setattr(queue, "Redis", RecordingRedis)
    monkeypatch.setattr(queue, "publish", lambda *_args: None)

    queue.enqueue_scheduled_once("redis://test", "check-drift", ttl_seconds=60)

    assert options == [
        {
            "socket_connect_timeout": queue._REDIS_OPERATION_TIMEOUT_SECONDS,
            "socket_timeout": queue._REDIS_OPERATION_TIMEOUT_SECONDS,
        }
    ]


def test_first_certificate_reconciliation_immediately_scans_automatic_ssl(
    monkeypatch,
):
    calls: list[tuple[str, str]] = []
    certificates = SimpleNamespace(
        reconcile_certificates=lambda operator: (
            calls.append(("certificates", operator))
            or SimpleNamespace(issued=("cdn-example-com",))
        ),
        renew_certificates=lambda _operator, **_kwargs: None,
    )
    automatic_ssl = SimpleNamespace(
        reconcile=lambda operator: calls.append(("automatic-ssl", operator))
    )
    deployments = SimpleNamespace(
        submit_deployment=lambda _operator: None,
        check_drift=lambda _operator: None,
    )
    control = SimpleNamespace(
        maintenance=MaintenanceService(
            certificates=certificates,
            automatic_ssl=automatic_ssl,
            deployments=deployments,
            requirements=SimpleNamespace(pending=lambda _kind: False),
            renewal_budget_seconds=300,
        ),
        close=lambda: None,
    )
    monkeypatch.setattr(
        "blitzecdn.control_plane.build_control_plane", lambda _settings: control
    )
    monkeypatch.setattr(worker, "release_schedule_key", lambda *_args: None)

    worker._run_control_plane("reconcile-certificates", "token")

    assert calls == [
        ("certificates", "scheduler"),
        ("automatic-ssl", "scheduler"),
    ]


def test_a_scheduled_actor_always_releases_its_key(monkeypatch):
    """A failing operation must not block the next tick for the key's whole TTL."""
    released: list[tuple[str, str]] = []
    control = SimpleNamespace(
        maintenance=SimpleNamespace(
            run=lambda _operation: (_ for _ in ()).throw(RuntimeError("drift failed"))
        ),
        close=lambda: None,
    )
    monkeypatch.setattr(
        "blitzecdn.control_plane.build_control_plane", lambda _settings: control
    )
    monkeypatch.setattr(
        worker,
        "release_schedule_key",
        lambda _url, operation, token: released.append((operation, token)),
    )

    with pytest.raises(RuntimeError, match="drift failed"):
        worker._run_control_plane("check-drift", "token")

    assert released == [("check-drift", "token")]
