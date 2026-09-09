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


class PluginError(BlitzeError):
    """A plugin could not be discovered, registered, or used.

    Distinct from `ConfigurationError` because the two are fixed in different
    places: a configuration error is a value an operator can change, while this
    one is a package that is installed, broken, or incompatible.
    """


class FailureKind(StrEnum):
    """What a failure *is*, named once so every delivery layer agrees.

    The CLI and the HTTP API both have to turn an exception into a number an
    unattended caller can branch on, and both used to carry their own ordered
    list of exception types to do it. The lists drifted, as two copies of one
    decision do: an unmapped `BlitzeError` exited 2 — `INVALID_INPUT`, "you
    typed it wrong" — on the command line while the same error answered 503 —
    "the service is down, retry" — over HTTP. Both cannot be right, and nothing
    failed when they disagreed because neither list knew the other existed.

    So the taxonomy lives here and the projections live in the delivery layers.
    Core names *what happened*; `blitzecdn.cli.main` decides what exit code says
    that to a shell and `blitzecdn.api.app` decides what status code says it to
    a client. Neither may invent a kind, and `tests/contract/test_delivery_parity.py`
    fails if either stops covering one.

    See docs/decisions/0006-how-a-failure-reaches-a-caller.md.
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
    #: The installation itself is wrong — a plugin that will not load, or a
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
    (PluginError, FailureKind.INTERNAL),
)


def classify(error: BlitzeError) -> FailureKind:
    """Name what went wrong, for a layer that has to answer with a number."""
    for kind, failure in _CLASSIFICATION:
        if isinstance(error, kind):
            return failure
    # A `BlitzeError` subclass added without a line above. INTERNAL rather than
    # a guess at the operator's mistake: we do not know it was one.
    return FailureKind.INTERNAL
