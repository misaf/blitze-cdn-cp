"""Application-safe exception hierarchy."""


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
