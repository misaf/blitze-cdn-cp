"""The ``Idempotency-Key`` header on queued (``202``) routes.

A client that loses a ``202`` cannot tell whether its work was queued. Sent
again with the same key, the request answers with the resource the first one
created instead of queueing the work twice. The key is checked before any
admission check, so a retry is answered even when the first request's work has
since changed the state those checks read.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Protocol

from fastapi import Depends, Header, Request

from blitzecdn.core.exceptions import ConflictError
from blitzecdn.core.ports.idempotency import IdempotencyKeyStore
from blitzecdn.delivery.api.dependencies import OperatorDependency

__all__ = ["IdempotencyKeyDependency", "IdempotentRequest", "idempotent"]


@dataclass(frozen=True, slots=True)
class IdempotentRequest:
    """A request sent with a key: who sent it, the key, and what it asked for."""

    operator: str
    key: str
    fingerprint: str


def _fingerprint(method: str, path: str, query: str, body: bytes) -> str:
    """The request's method, path, query and body, so a reused key is detected.

    A JSON body is compared by value, not by its spacing or key order.
    """
    try:
        canonical = json.dumps(
            json.loads(body), sort_keys=True, separators=(",", ":")
        ).encode()
    except ValueError:
        canonical = body
    digest = hashlib.sha256()
    for part in (method.encode(), path.encode(), query.encode(), canonical):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


async def idempotent_request(
    request: Request,
    operator: OperatorDependency,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=255,
            pattern=r"^[\x21-\x7e]+$",
            description=(
                "A unique value per intended action, such as a UUID. Repeating "
                "the request with the same key within 24 hours returns the "
                "resource the first one queued instead of queueing it again. "
                "Keys are per API key; reusing one for a different request is "
                "`409`."
            ),
        ),
    ] = None,
) -> IdempotentRequest | None:
    if idempotency_key is None:
        return None
    return IdempotentRequest(
        operator=operator,
        key=idempotency_key,
        fingerprint=_fingerprint(
            request.method,
            request.url.path,
            request.url.query,
            await request.body(),
        ),
    )


IdempotencyKeyDependency = Annotated[
    IdempotentRequest | None, Depends(idempotent_request)
]


class _Identified(Protocol):
    @property
    def id(self) -> str: ...


def idempotent[T: _Identified](
    request: IdempotentRequest | None,
    keys: IdempotencyKeyStore,
    *,
    create: Callable[[], T],
    replay: Callable[[str], T],
) -> T:
    """Run ``create`` once per key, answering a repeat with ``replay`` of its id.

    ``create`` includes the route's admission checks. If it raises, nothing was
    queued and the key is released, so a retry with it may succeed later.
    """
    if request is None:
        return create()
    held = keys.reserve(request.operator, request.key, request.fingerprint)
    if held is not None:
        if held.fingerprint != request.fingerprint:
            raise ConflictError(
                "this Idempotency-Key was already used for a different request; "
                "send a new key for new work"
            )
        if held.resource_id is None:
            raise ConflictError(
                "a request with this Idempotency-Key is still being admitted; "
                "retry it shortly"
            )
        return replay(held.resource_id)
    try:
        created = create()
    except BaseException:
        keys.release(request.operator, request.key)
        raise
    keys.bind(request.operator, request.key, created.id)
    return created
