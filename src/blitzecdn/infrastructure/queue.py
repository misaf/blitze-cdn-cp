"""Dramatiq actors for durable, out-of-process control-plane work."""

from __future__ import annotations

import logging
import os

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from blitzecdn.config import Settings

_LOGGER = logging.getLogger(__name__)
_broker_url: str | None = None


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
        "renew_certificates",
        "check_drift",
    ):
        actor = globals().get(name)
        if actor is not None:
            actor.broker = broker
    _broker_url = redis_url


# The worker imports this module before accepting messages. systemd supplies
# the same environment file as the API, while tests configure explicitly.
configure_broker(os.environ.get("BLITZE_REDIS_URL", "redis://127.0.0.1:6379/0"))


@dramatiq.actor(max_retries=0, queue_name="deployments")
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


def _run_control_plane(operation: str) -> None:
    """Run one scheduled operation in the worker process."""
    from blitzecdn.control_plane import build_control_plane

    control = build_control_plane(Settings.from_environment())
    try:
        if operation == "certificate-reconciliation":
            control.certificates.reconcile_certificates("scheduler")
        elif operation == "certificate-renewal":
            result = control.certificates.renew_certificates(
                "scheduler",
                budget_seconds=control.settings.certificate_renewal_budget_seconds,
            )
            if result.renewed:
                control.deployments.submit_deployment("scheduler")
        elif operation == "drift-check":
            control.deployments.check_drift("scheduler")
        else:  # pragma: no cover - actors below provide the closed set
            raise ValueError(f"unknown scheduled operation: {operation}")
    finally:
        control.close()


@dramatiq.actor(max_retries=0, queue_name="scheduled")
def reconcile_certificates() -> None:
    _run_control_plane("certificate-reconciliation")


@dramatiq.actor(max_retries=0, queue_name="scheduled")
def renew_certificates() -> None:
    _run_control_plane("certificate-renewal")


@dramatiq.actor(max_retries=0, queue_name="scheduled")
def check_drift() -> None:
    _run_control_plane("drift-check")
