"""The error responses an operation declares, beside the 422 in `validation`.

Every failure but request validation answers ``{"detail": "message"}``: the
`BlitzeError` handler in `entrypoints.api.app`, `require_operator`, and the
access and body-limit middleware all write that shape. These dictionaries put
it in the published specification, per status, so generated clients know an
operation can answer 404 or 409. `tests/api/test_error_responses.py` checks
each operation's declarations against what its route depends on.
"""

from typing import Any

from blitzecdn.delivery.api.models import Model


class ErrorResponse(Model):
    detail: str


_ERROR_CONTENT: dict[str, Any] = {
    "application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}
}


def _error(description: str, **headers: dict[str, Any]) -> dict[str, Any]:
    # Explicit JSON content, as for 422, so a route whose success is text
    # still documents its errors as JSON.
    response: dict[str, Any] = {"description": description, "content": _ERROR_CONTENT}
    if headers:
        response["headers"] = headers
    return response


#: Answered by any operation: the IP gate runs before routing, and a failure
#: the controller did not anticipate is its own fault.
GLOBAL_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: _error("The client's IP address is not in `allowed_ips`."),
    500: _error("The installation is wrong; retrying will not help."),
}

#: Every operation behind `require_operator`.
OPERATOR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: _error(
        "The X-API-Key header is missing or matches no operator.",
        **{
            "WWW-Authenticate": {
                "description": "Always `ApiKey`.",
                "schema": {"type": "string", "enum": ["ApiKey"]},
            }
        },
    ),
    503: _error("No API keys are configured, so no request can authenticate."),
}

#: An operation that names a resource, in its path or its body.
NOT_FOUND_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: _error("A resource the request names does not exist."),
}

#: A write that can contradict current state: a duplicate, a stale edit, work
#: already queued for the same target, or an `Idempotency-Key` reused for a
#: different request.
CONFLICT_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: _error("The request conflicts with current state."),
}

#: An operation that reads a request body, which the body limit bounds.
BODY_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: _error("The Content-Length header is not a valid length."),
    413: _error("The request body exceeds `api_max_body_bytes`."),
}
