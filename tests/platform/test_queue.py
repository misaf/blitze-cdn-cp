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
from blitzecdn.capabilities.maintenance import MaintenanceService
from blitzecdn.core import broker as queue
from blitzecdn.core.exceptions import DeploymentBusyError, NotFoundError
from blitzecdn.core.plugins import ScheduledJob
from blitzecdn.worker import run_deployment, run_scheduled_job


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
    """Two actors, and every scheduled job of every plugin goes through one.

    A job's name travels in the message rather than in the actor name, which is
    what lets a plugin this repository has never heard of contribute recurring
    work: there is nothing to declare here for it.
    """
    broker = StubBroker()
    dramatiq.set_broker(broker)
    actors = (run_deployment, run_scheduled_job)
    previous = [actor.broker for actor in actors]
    try:
        for actor in actors:
            actor.broker = broker
            broker.declare_actor(actor)
        run_deployment.send("deployment-id")
        run_scheduled_job.send("check-drift", "drift-token")
        run_scheduled_job.send("waf-rule-refresh", "waf-token")

        deployment = dramatiq.Message.decode(broker.queues["deployments"].get_nowait())
        scheduled = [
            dramatiq.Message.decode(broker.queues["scheduled"].get_nowait())
            for _ in range(2)
        ]
        assert deployment.args == ("deployment-id",)
        assert {message.actor_name for message in scheduled} == {"run_scheduled_job"}
        assert {message.args for message in scheduled} == {
            ("check-drift", "drift-token"),
            ("waf-rule-refresh", "waf-token"),
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
    assert run_scheduled_job.actor_name == queue.RUN_SCHEDULED_JOB_ACTOR
    assert run_scheduled_job.queue_name == queue.SCHEDULED_QUEUE


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
    assert actor_name == queue.RUN_SCHEDULED_JOB_ACTOR
    assert queue_name == queue.SCHEDULED_QUEUE
    assert args == (
        "check-drift",
        FakeRedis.values[f"{queue._SCHEDULE_KEY_PREFIX}check-drift"],
    )


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


def test_a_maintenance_run_converges_what_it_left_owing(monkeypatch):
    """The one rule that holds across every scheduled job, whoever wrote it.

    A job that changed something the fleet has not seen raises a deployment
    requirement, and the run that raised it is the run that should pay it off —
    otherwise the change sits in the database until an operator happens to
    deploy for an unrelated reason.
    """
    calls: list[str] = []
    service = MaintenanceService(
        jobs=lambda: {
            "renew-certificates": ScheduledJob(
                name="renew-certificates",
                interval_seconds=60,
                run=lambda operator: calls.append(f"renewed by {operator}"),
            )
        },
        deployments=SimpleNamespace(
            submit_deployment=lambda operator: calls.append(f"deployed by {operator}")
        ),
        requirements=SimpleNamespace(pending=lambda _kind: True),
    )

    service.run("renew-certificates")

    assert calls == ["renewed by scheduler", "deployed by scheduler"]


def test_a_job_no_installed_plugin_contributes_is_refused_by_name():
    """A message can outlive the plugin that published it."""
    service = MaintenanceService(
        jobs=lambda: {},
        deployments=SimpleNamespace(submit_deployment=lambda _operator: None),
        requirements=SimpleNamespace(pending=lambda _kind: False),
    )

    with pytest.raises(NotFoundError, match="no scheduled job named 'check-drift'"):
        service.run("check-drift")


def test_a_scheduled_actor_always_releases_its_key(monkeypatch):
    """A failing job must not block the next tick for the key's whole TTL."""
    released: list[tuple[str, str]] = []
    control = SimpleNamespace(
        maintenance=SimpleNamespace(
            run=lambda _job: (_ for _ in ()).throw(RuntimeError("drift failed"))
        ),
        close=lambda: None,
    )
    monkeypatch.setattr(
        "blitzecdn.bootstrap.build_control_plane",
        lambda _settings, **_kwargs: control,
    )
    monkeypatch.setattr(
        worker,
        "release_schedule_key",
        lambda _url, job, token: released.append((job, token)),
    )

    with pytest.raises(RuntimeError, match="drift failed"):
        worker.run_scheduled_job("check-drift", "token")

    assert released == [("check-drift", "token")]
