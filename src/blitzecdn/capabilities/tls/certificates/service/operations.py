"""Certificate issuance, renewal and reconciliation as queued operations.

Each waits on the CA, DNS and HTTP preflight probes, so the HTTP API queues it
for the worker instead of holding a request open; the CLI calls the service
directly and waits. A payload is JSON naming the operator and the request; a
run returns the same report the synchronous call does, as JSON.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from blitzecdn.capabilities.tls.certificates.service.issuance import (
    CertificateService,
)

__all__ = [
    "OPERATION_KINDS",
    "RECONCILE_DEDUPE_KEY",
    "RECONCILE_OPERATION",
    "RENEW_DEDUPE_KEY",
    "RENEW_OPERATION",
    "REQUEST_OPERATION",
    "operation_handlers",
    "reconcile_payload",
    "renew_payload",
    "request_dedupe_key",
    "request_payload",
]

REQUEST_OPERATION = "certificates.request"
RENEW_OPERATION = "certificates.renew"
RECONCILE_OPERATION = "certificates.reconcile"
OPERATION_KINDS = (REQUEST_OPERATION, RENEW_OPERATION, RECONCILE_OPERATION)

#: One sweep at a time: a second would only wait on the first's lock, then
#: find nothing left to do.
RENEW_DEDUPE_KEY = RENEW_OPERATION
RECONCILE_DEDUPE_KEY = RECONCILE_OPERATION


def request_payload(
    operator: str, *, name: str, email: str | None, skip_preflight: bool
) -> dict[str, Any]:
    return {
        "operator": operator,
        "name": name,
        "email": email,
        "skip_preflight": skip_preflight,
    }


def request_dedupe_key(name: str) -> str:
    """One request per site in flight: a second would ask the CA again."""
    return f"{REQUEST_OPERATION}:{name}"


def renew_payload(
    operator: str,
    *,
    within_days: int,
    force: bool,
    sites: Sequence[str] | None,
) -> dict[str, Any]:
    return {
        "operator": operator,
        "within_days": within_days,
        "force": force,
        "sites": None if sites is None else list(sites),
    }


def reconcile_payload(operator: str) -> dict[str, Any]:
    return {"operator": operator}


def operation_handlers(
    certificates: CertificateService, *, renewal_budget_seconds: float
) -> dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]]:
    """Each kind's run, from its payload to its JSON report.

    A renewal sweep stops between sites once ``renewal_budget_seconds`` has
    passed, so one job cannot outlive the worker's lease on it.
    """

    def request(payload: Mapping[str, Any]) -> dict[str, Any]:
        info = certificates.request_certificate(
            str(payload["name"]),
            str(payload["operator"]),
            payload.get("email"),
            skip_preflight=bool(payload["skip_preflight"]),
        )
        return info.model_dump(mode="json")

    def renew(payload: Mapping[str, Any]) -> dict[str, Any]:
        sites = payload.get("sites")
        result = certificates.renew_certificates(
            str(payload["operator"]),
            within_days=int(payload["within_days"]),
            force=bool(payload["force"]),
            sites=None if sites is None else [str(site) for site in sites],
            budget_seconds=renewal_budget_seconds,
        )
        return result.model_dump(mode="json")

    def reconcile(payload: Mapping[str, Any]) -> dict[str, Any]:
        # An operator asking now has usually just fixed something, so every
        # waiting site is checked, even one the scheduled run is backing off.
        result = certificates.reconcile_certificates(
            str(payload["operator"]), force=True
        )
        return result.model_dump(mode="json")

    return {
        REQUEST_OPERATION: request,
        RENEW_OPERATION: renew,
        RECONCILE_OPERATION: reconcile,
    }
