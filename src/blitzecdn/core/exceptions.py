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
    """A feature could not be discovered, registered, or used.

    Distinct from `ConfigurationError` because the two are fixed in different
    places: a configuration error is a value an operator can change, while this
    one is a package that is installed, broken, or incompatible.
    """


class FailureKind(StrEnum):
    """What a failure *is*, named once so every delivery layer agrees.

    The CLI and the HTTP API both have to turn an exception into a number an
    unattended caller can branch on. Two lists of exception types would drift,
    as two copies of one decision do — the same error "you typed it wrong" on
    the command line and "the service is down, retry" over HTTP — so the kind
    is decided once, here, and each layer only projects it.

    So the taxonomy lives here and the projections live in the delivery layers.
    Core names *what happened*; `blitzecdn.cli.main` decides what exit code says
    that to a shell and `blitzecdn.api.app` decides what status code says it to
    a client. Neither may invent a kind, and `tests/contract/test_delivery_parity.py`
    fails if either stops covering one.
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
    #: The installation itself is wrong — a feature that will not load, or a
    #: `BlitzeError` no layer anticipated. Distinct from CONFIGURATION because
    #: retrying cannot help and the fix is a package, not a setting.
    INTERNAL = "internal"


#: Exception type to kind, walked most-specific first because
#: `DeploymentBusyError` is a `ConflictError` and would otherwise be matched by
#: its parent. Order is load-bearing; keep subclasses above their bases.
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
