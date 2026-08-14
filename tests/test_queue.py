"""Dramatiq queue contracts without a live Redis server."""

from __future__ import annotations

from typing import ClassVar

import dramatiq
import pytest
from dramatiq.brokers.stub import StubBroker

from blitzecdn.exceptions import DeploymentBusyError
from blitzecdn.infrastructure import queue
from blitzecdn.infrastructure.queue import (
    check_drift,
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
    actors = (run_deployment, reconcile_certificates, renew_certificates, check_drift)
    previous = [actor.broker for actor in actors]
    try:
        for actor in actors:
            actor.broker = broker
            broker.declare_actor(actor)
        run_deployment.send("deployment-id")
        reconcile_certificates.send("reconcile-token")
        renew_certificates.send("renew-token")
        check_drift.send("drift-token")

        deployment = dramatiq.Message.decode(broker.queues["deployments"].get_nowait())
        scheduled = [
            dramatiq.Message.decode(broker.queues["scheduled"].get_nowait())
            for _ in range(3)
        ]
        assert deployment.args == ("deployment-id",)
        assert {message.actor_name for message in scheduled} == {
            "reconcile_certificates",
            "renew_certificates",
            "check_drift",
        }
        assert {message.args for message in scheduled} == {
            ("reconcile-token",),
            ("renew-token",),
            ("drift-token",),
        }
    finally:
        for actor, original in zip(actors, previous, strict=True):
            actor.broker = original


def test_scheduled_enqueue_is_atomic_and_passes_ownership_token(monkeypatch):
    FakeRedis.values.clear()
    sent: list[str] = []
    monkeypatch.setattr(queue, "Redis", FakeRedis)
    monkeypatch.setattr(queue.check_drift, "send", lambda token: sent.append(token))

    assert queue.enqueue_scheduled_once("redis://test", "check-drift", ttl_seconds=60)
    assert not queue.enqueue_scheduled_once(
        "redis://test", "check-drift", ttl_seconds=60
    )
    assert len(sent) == 1


def test_failed_scheduled_publish_releases_its_key(monkeypatch):
    FakeRedis.values.clear()
    monkeypatch.setattr(queue, "Redis", FakeRedis)

    def fail(_token: str) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(queue.check_drift, "send", fail)
    with pytest.raises(RuntimeError, match="broker unavailable"):
        queue.enqueue_scheduled_once("redis://test", "check-drift", ttl_seconds=60)

    assert FakeRedis.values == {}


def test_deployment_actor_retries_only_a_busy_fleet_lock():
    retry_when = run_deployment.options["retry_when"]

    assert retry_when(0, DeploymentBusyError("busy"))
    assert not retry_when(queue._DEPLOYMENT_LOCK_RETRIES, DeploymentBusyError("busy"))
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
    monkeypatch.setattr(queue.check_drift, "send", lambda _token: None)

    queue.enqueue_scheduled_once("redis://test", "check-drift", ttl_seconds=60)

    assert options == [
        {
            "socket_connect_timeout": queue._REDIS_OPERATION_TIMEOUT_SECONDS,
            "socket_timeout": queue._REDIS_OPERATION_TIMEOUT_SECONDS,
        }
    ]
