"""Dramatiq queue contracts without a live Redis server."""

from __future__ import annotations

import dramatiq
from dramatiq.brokers.stub import StubBroker

from blitzecdn.infrastructure.queue import (
    check_drift,
    reconcile_certificates,
    renew_certificates,
    run_deployment,
)


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
        reconcile_certificates.send()
        renew_certificates.send()
        check_drift.send()

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
    finally:
        for actor, original in zip(actors, previous, strict=True):
            actor.broker = original
