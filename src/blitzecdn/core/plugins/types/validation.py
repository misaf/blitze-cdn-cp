"""What a capability knows about a site that a deployment should hear.

Answered by ``blitzecdn_deployment_checks``, before anything is rendered.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "Severity",
    "ValidationIssue",
    "ValidationResult",
]


class Severity(StrEnum):
    BLOCKING = "blocking"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """Something a plugin knows about a site that a deployment should hear.

    A blocking issue refuses the deployment before anything is rendered; a
    warning is reported and converged anyway.
    """

    plugin: str
    site: str
    message: str
    severity: Severity = Severity.BLOCKING


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Every plugin's answer about one site, and whether it may be deployed."""

    site: str
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def blocking(self) -> tuple[ValidationIssue, ...]:
        return tuple(
            issue for issue in self.issues if issue.severity is Severity.BLOCKING
        )

    @property
    def ok(self) -> bool:
        return not self.blocking
