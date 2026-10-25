"""Application-safe exception hierarchy, and how a failure reaches a caller."""

from enum import StrEnum


class BlitzeError(Exception):
    """Base class for errors safe to present to an operator."""


class ConfigurationError(BlitzeError):
    """Configuration is missing, malformed, or unsafe."""


class ConflictError(BlitzeError):
    """The requested operation conflicts with current state."""


class NotFoundError(BlitzeError):
    """A requested resource does not exist."""


class DeploymentBusyError(ConflictError):
    """Another deployment owns the process-wide deployment lock."""


class ExecutionError(BlitzeError):
    """An infrastructure command could not be executed."""


class FeatureError(BlitzeError):
    """A feature could not be loaded, registered, or used.

    Unlike `ConfigurationError`, the fix is in the code, not in a value an
    operator can change.
    """


class FailureKind(StrEnum):
    """What a failure is, decided once so the CLI and the API agree.

    Each delivery layer projects a kind onto its own number — an exit code in
    `blitzecdn.entrypoints.cli.main`, a status code in `blitzecdn.entrypoints.api.app`
    — and `tests/contract/test_delivery_parity.py` fails if either misses one.
    """

    #: Another deployment holds the lock. The one failure that clears on its
    #: own, so the one a scheduled caller should retry rather than alert on.
    BUSY = "busy"
    #: The request contradicts current state.
    CONFLICT = "conflict"
    #: The thing asked about does not exist.
    NOT_FOUND = "not_found"
    #: An infrastructure command ran and did not succeed. The controller is
    #: fine; what it was driving is not.
    EXECUTION = "execution"
    #: A value an operator can change is missing, malformed, or unsafe.
    CONFIGURATION = "configuration"
    #: The installation is wrong — a feature that will not load, or an
    #: unanticipated `BlitzeError`. Retrying cannot help.
    INTERNAL = "internal"


#: Exception type to kind, most specific first: keep subclasses above their bases.
_CLASSIFICATION: tuple[tuple[type[BlitzeError], FailureKind], ...] = (
    (DeploymentBusyError, FailureKind.BUSY),
    (ConflictError, FailureKind.CONFLICT),
    (NotFoundError, FailureKind.NOT_FOUND),
    (ExecutionError, FailureKind.EXECUTION),
    (ConfigurationError, FailureKind.CONFIGURATION),
    (FeatureError, FailureKind.INTERNAL),
)


def classify(error: BlitzeError) -> FailureKind:
    """Name what went wrong, for a layer that has to answer with a number."""
    for kind, failure in _CLASSIFICATION:
        if isinstance(error, kind):
            return failure
    # A `BlitzeError` subclass added without a line above. INTERNAL rather than
    # a guess at the operator's mistake: we do not know it was one.
    return FailureKind.INTERNAL
