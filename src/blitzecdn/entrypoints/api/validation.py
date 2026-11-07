"""One HTTP representation for request and application validation failures."""

from fastapi import status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from blitzecdn.entrypoints.api.models import Model


class ValidationIssue(Model):
    type: str
    loc: list[str | int]
    msg: str


class ValidationErrorResponse(Model):
    detail: list[ValidationIssue]


# Explicit JSON content also works on routes whose successful response is text.
VALIDATION_RESPONSE = {
    "description": "Request validation failed.",
    "content": {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/ValidationErrorResponse"}
        }
    },
}


def validation_response(exc: RequestValidationError | ValidationError) -> JSONResponse:
    if isinstance(exc, RequestValidationError):
        errors = exc.errors()
        prefix: tuple[str, ...] = ()
    else:
        # Domain validation after a body is merged has no HTTP location yet.
        errors = exc.errors(
            include_url=False, include_context=False, include_input=False
        )
        prefix = ("body",)
    body = ValidationErrorResponse(
        detail=[
            ValidationIssue(
                type=error["type"],
                loc=[*prefix, *error["loc"]],
                msg=error["msg"],
            )
            for error in errors
        ]
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=body.model_dump(mode="json"),
    )
