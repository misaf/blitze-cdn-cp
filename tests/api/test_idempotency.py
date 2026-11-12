"""A queued request repeated with its ``Idempotency-Key`` is answered, not re-queued."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from support.control_plane import API_HEADERS, control_plane_app, seed_edge, serve
from support.stores import Stores

from blitzecdn.capabilities.cache.service.operations import CacheConnectionsJob
from blitzecdn.capabilities.jobs.domain import Job
from blitzecdn.capabilities.jobs.service import JobQueue, OperationService
from blitzecdn.core.exceptions import ConflictError
from blitzecdn.core.persistence.idempotency import IdempotencyKeys
from blitzecdn.core.ports.clock import system_clock
from blitzecdn.core.ports.idempotency import KeyRecord, Reservation
from blitzecdn.delivery.api.idempotency import (
    IdempotentRequest,
    _fingerprint,
    idempotent,
)

#: Every queued route, with a body it admits.
SUBMISSIONS = [
    ("POST", "/v3/cache/purge", {"purge_all": True}),
    ("POST", "/v3/cache/connections", {}),
    ("POST", "/v3/origins/check", {}),
    ("DELETE", "/v3/edges/edge-a", None),
    ("POST", "/v3/deployments", {}),
    ("POST", "/v3/deployments", {"check": True}),
    ("POST", "/v3/deployments/rollback", {}),
    ("POST", "/v3/certificates/renew", {}),
    ("POST", "/v3/certificates/reconcile", None),
    ("POST", "/v3/sites/example-com/certificate/request", {"email": "ops@example.com"}),
]


def _keyed(key: str) -> dict[str, str]:
    return {**API_HEADERS, "Idempotency-Key": key}


@pytest.mark.parametrize(("method", "path", "body"), SUBMISSIONS)
def test_a_repeated_key_returns_the_first_resource_and_queues_nothing(
    settings, seeded, monkeypatch, method, path, body
):
    control, repository = seeded()
    seed_edge(control)
    if path == "/v3/deployments/rollback":
        body = {"deployment_id": control.deployments.deploy("tester").id}
    serve(monkeypatch, control)

    with TestClient(control_plane_app(settings)) as client:
        first = client.request(method, path, json=body, headers=_keyed("retry-1"))
        assert first.status_code == 202, first.text
        queued = len(repository.jobs.list_jobs(limit=1000))

        again = client.request(method, path, json=body, headers=_keyed("retry-1"))

        assert again.status_code == 202, again.text
        assert again.json()["id"] == first.json()["id"]
        assert again.headers["Location"] == first.headers["Location"]
        assert again.headers["Retry-After"] == "2"
        assert len(repository.jobs.list_jobs(limit=1000)) == queued


def test_without_a_key_each_request_queues_its_own_work(settings, seeded, monkeypatch):
    control, _ = seeded()
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        first = client.post("/v3/cache/connections", json={}, headers=API_HEADERS)
        second = client.post("/v3/cache/connections", json={}, headers=API_HEADERS)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] != second.json()["id"]


def test_a_key_reused_for_a_different_request_is_refused(settings, seeded, monkeypatch):
    control, _ = seeded()
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        first = client.post("/v3/cache/connections", json={}, headers=_keyed("reused"))
        other = client.post(
            "/v3/cache/connections",
            json={"host_limit": "edge-a"},
            headers=_keyed("reused"),
        )
        elsewhere = client.post("/v3/origins/check", json={}, headers=_keyed("reused"))
    assert first.status_code == 202
    assert other.status_code == elsewhere.status_code == 409
    assert "different request" in other.json()["detail"]
    assert "Location" not in other.headers


def test_a_json_body_matches_by_value_not_by_spelling(settings, seeded, monkeypatch):
    control, _ = seeded()
    serve(monkeypatch, control)
    headers = {**_keyed("spelling"), "Content-Type": "application/json"}
    with TestClient(control_plane_app(settings)) as client:
        first = client.post(
            "/v3/cache/purge", content=b'{"purge_all": true}', headers=headers
        )
        again = client.post(
            "/v3/cache/purge", content=b'{ "purge_all":true }', headers=headers
        )
    assert first.status_code == again.status_code == 202
    assert again.json()["id"] == first.json()["id"]


def test_a_refused_request_leaves_its_key_free_for_a_retry(
    settings, seeded, monkeypatch
):
    control, _ = seeded()
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        refused = client.post("/v3/cache/purge", json={}, headers=_keyed("free"))
        assert refused.status_code == 409
        # The same request again is checked again, not answered from the key.
        assert (
            client.post("/v3/cache/purge", json={}, headers=_keyed("free")).status_code
            == 409
        )
        accepted = client.post(
            "/v3/cache/purge", json={"purge_all": True}, headers=_keyed("free")
        )
    assert accepted.status_code == 202


def test_a_key_still_being_admitted_answers_conflict(settings, seeded, monkeypatch):
    control, repository = seeded()
    serve(monkeypatch, control)
    # Reserved and not yet bound, as by a first try still being admitted.
    fingerprint = _fingerprint("POST", "/v3/cache/connections", "", b"{}")
    assert isinstance(
        repository.idempotency_keys.reserve("tester", "in-flight", fingerprint),
        Reservation,
    )
    with TestClient(control_plane_app(settings)) as client:
        busy = client.post(
            "/v3/cache/connections", json={}, headers=_keyed("in-flight")
        )
    assert busy.status_code == 409
    assert "still being admitted" in busy.json()["detail"]


@pytest.mark.parametrize("key", ["", "has space", "x" * 256, "naïve"])
def test_a_malformed_key_is_a_validation_error(settings, seeded, monkeypatch, key):
    control, _ = seeded()
    serve(monkeypatch, control)
    with TestClient(control_plane_app(settings)) as client:
        response = client.post(
            "/v3/cache/connections",
            json={},
            headers={**API_HEADERS, "Idempotency-Key": key.encode()},
        )
    assert response.status_code == 422


def test_openapi_documents_the_header_on_every_queued_route(settings):
    document = control_plane_app(settings).openapi()
    for path, methods in document["paths"].items():
        for operation in methods.values():
            parameters = {
                parameter["name"] for parameter in operation.get("parameters", [])
            }
            queued = "202" in operation.get("responses", {})
            assert ("Idempotency-Key" in parameters) == queued, path


def test_expired_keys_and_abandoned_reservations_reserve_afresh(seeded):
    _, repository = seeded()
    database = repository.database
    keys = IdempotencyKeys(database)
    reservation = keys.reserve("tester", "kept", "a")
    assert isinstance(reservation, Reservation)
    with database.transaction():
        keys.bind(reservation, "job-1")
    assert keys.reserve("tester", "kept", "a") == KeyRecord("a", "job-1")
    # Keys are per operator.
    assert isinstance(keys.reserve("someone-else", "kept", "a"), Reservation)

    expired = IdempotencyKeys(database, lifetime=timedelta(seconds=-1))
    assert isinstance(expired.reserve("tester", "kept", "a"), Reservation)

    keys.reserve("tester", "stuck", "a")
    assert keys.reserve("tester", "stuck", "a") == KeyRecord("a", None)
    abandoned = IdempotencyKeys(database, reservation_timeout=timedelta(seconds=-1))
    assert isinstance(abandoned.reserve("tester", "stuck", "a"), Reservation)


def test_a_key_is_bound_only_inside_the_transaction_that_queues(seeded):
    _, repository = seeded()
    keys = repository.idempotency_keys
    reservation = keys.reserve("tester", "outside", "a")
    assert isinstance(reservation, Reservation)
    with pytest.raises(ValueError, match="Unit of Work"):
        keys.bind(reservation, "job-1")


# -- Durability across a stopped process --------------------------------------


class _ProcessDied(BaseException):
    """The process stopping mid-request: no handler runs, nothing is cleaned up."""


class _DeadKeys:
    """Keys as a process that dies leaves them: it never releases a reservation."""

    def __init__(self, keys: IdempotencyKeys, *, die_on_bind: bool = False) -> None:
        self._keys = keys
        self._die_on_bind = die_on_bind

    def reserve(self, operator: str, key: str, fingerprint: str):
        return self._keys.reserve(operator, key, fingerprint)

    def bind(self, reservation: Reservation, resource_id: str) -> None:
        self._keys.bind(reservation, resource_id)
        if self._die_on_bind:
            raise _ProcessDied

    def release(self, reservation: Reservation) -> None:
        pass


REQUEST = IdempotentRequest(operator="tester", key="durable", fingerprint="f")


@pytest.fixture
def stores(seeded) -> Stores:
    _, repository = seeded()
    return repository


@pytest.fixture
def operations(stores: Stores) -> OperationService:
    queue = JobQueue(
        jobs=stores.jobs, handlers=(), worker_id="worker-a", clock=system_clock
    )
    return OperationService(
        queue=queue, kinds={"cache.connections"}, uow=stores.database
    )


def _queued(stores: Stores) -> list[Job]:
    return [
        job
        for job in stores.jobs.list_jobs(limit=1000)
        if job.kind == "cache.connections"
    ]


def _submit(operations: OperationService):
    return lambda: operations.submit(CacheConnectionsJob(operator="tester"))


def _abandoned(stores: Stores) -> IdempotencyKeys:
    """The keys as a retry sees them once a reservation has timed out."""
    return IdempotencyKeys(stores.database, reservation_timeout=timedelta(seconds=-1))


def test_a_process_stopped_after_queueing_has_already_bound_its_key(stores, operations):
    """The regression: the job committed, then the process died before replying.

    The key was bound in the job's transaction, so a retry after the reservation
    timeout replays the job instead of queueing a second.
    """

    def submit_then_die():
        _submit(operations)()
        raise _ProcessDied

    with pytest.raises(_ProcessDied):
        idempotent(
            REQUEST,
            _DeadKeys(stores.idempotency_keys),
            create=submit_then_die,
            replay=operations.get,
        )
    (first,) = _queued(stores)

    retried = idempotent(
        REQUEST,
        _abandoned(stores),
        create=_submit(operations),
        replay=operations.get,
    )

    assert retried.id == first.id
    assert len(_queued(stores)) == 1


def test_a_process_stopped_while_binding_queued_nothing(stores, operations):
    """Dying inside the transaction rolls the job back with the key's binding.

    Until the reservation times out a retry is told the first is in progress;
    after, it queues the work exactly once.
    """
    with pytest.raises(_ProcessDied):
        idempotent(
            REQUEST,
            _DeadKeys(stores.idempotency_keys, die_on_bind=True),
            create=_submit(operations),
            replay=operations.get,
        )
    assert _queued(stores) == []

    with pytest.raises(ConflictError, match="still being admitted"):
        idempotent(
            REQUEST,
            stores.idempotency_keys,
            create=_submit(operations),
            replay=operations.get,
        )

    retried = idempotent(
        REQUEST, _abandoned(stores), create=_submit(operations), replay=operations.get
    )
    again = idempotent(
        REQUEST,
        _abandoned(stores),
        create=_submit(operations),
        replay=operations.get,
    )
    assert again.id == retried.id
    assert [job.id for job in _queued(stores)] == [retried.id]


def test_a_retry_during_admission_is_told_to_wait_then_replays(stores, operations):
    keys = stores.idempotency_keys
    answers: list[str] = []

    def submit_while_retried():
        with pytest.raises(ConflictError, match="still being admitted"):
            idempotent(REQUEST, keys, create=_submit(operations), replay=operations.get)
        answers.append("in progress")
        return _submit(operations)()

    first = idempotent(
        REQUEST, keys, create=submit_while_retried, replay=operations.get
    )
    again = idempotent(REQUEST, keys, create=_submit(operations), replay=operations.get)

    assert answers == ["in progress"]
    assert again.id == first.id
    assert len(_queued(stores)) == 1


def test_a_request_whose_reservation_was_taken_over_queues_nothing(stores, operations):
    """A first try slower than the reservation timeout loses to its retry.

    Both reach their transaction; only the retry's commits, and the first try's
    rollback does not release the key the retry now holds.
    """
    retry: list[Job] = []

    def submit_after_a_takeover():
        retry.append(
            idempotent(
                REQUEST,
                _abandoned(stores),
                create=_submit(operations),
                replay=operations.get,
            )
        )
        return _submit(operations)()

    with pytest.raises(ConflictError, match="taken over"):
        idempotent(
            REQUEST,
            stores.idempotency_keys,
            create=submit_after_a_takeover,
            replay=operations.get,
        )

    (queued,) = _queued(stores)
    assert queued.id == retry[0].id
    replayed = idempotent(
        REQUEST,
        stores.idempotency_keys,
        create=_submit(operations),
        replay=operations.get,
    )
    assert replayed.id == queued.id


def test_releasing_a_lost_reservation_keeps_the_one_that_replaced_it(stores):
    keys = stores.idempotency_keys
    lost = keys.reserve("tester", "replaced", "f")
    assert isinstance(lost, Reservation)
    current = _abandoned(stores).reserve("tester", "replaced", "f")
    assert isinstance(current, Reservation)
    assert current != lost

    keys.release(lost)

    assert keys.reserve("tester", "replaced", "f") == KeyRecord("f", None)


def test_a_refused_admission_queues_nothing_and_frees_the_key(stores, operations):
    def refuse() -> Job:
        raise ConflictError("refused")

    with pytest.raises(ConflictError, match="refused"):
        idempotent(
            REQUEST, stores.idempotency_keys, create=refuse, replay=operations.get
        )
    assert _queued(stores) == []
    assert isinstance(
        stores.idempotency_keys.reserve("tester", "durable", "f"), Reservation
    )


def test_a_queued_resource_that_skips_binding_is_refused(stores, operations):
    """A service that writes a queued resource must bind the key with it."""
    unbound = operations.queue.enqueue(CacheConnectionsJob(operator="tester"))
    with pytest.raises(RuntimeError, match="without binding"):
        idempotent(
            REQUEST,
            stores.idempotency_keys,
            create=lambda: unbound,
            replay=operations.get,
        )
