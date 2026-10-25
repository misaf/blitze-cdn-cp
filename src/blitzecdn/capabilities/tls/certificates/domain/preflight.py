"""What is checked about the outside world before a certificate is asked for."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = [
    "TTL_CUTOVER_ADVISORY_SECONDS",
    "PreflightCheck",
    "PreflightReport",
    "PreflightSeverity",
]


#: A proxied record with a TTL above this is flagged: around a cutover,
#: resolvers keep the old address for the TTL, and HTTP-01 follows public DNS.
TTL_CUTOVER_ADVISORY_SECONDS = 3600


class PreflightSeverity(StrEnum):
    """Whether a failed check stops issuance or is merely reported.

    ``BLOCKING`` only where HTTP-01 cannot succeed; anything a certificate can be
    issued despite is ``ADVISORY``.
    """

    BLOCKING = "blocking"
    ADVISORY = "advisory"


class PreflightCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Stable identifier (``dns``, ``caa``, ``deployed``, ``origin``, ``ttl``),
    #: safe for a caller to branch on. ``detail`` is prose and is not.
    name: str
    passed: bool
    severity: PreflightSeverity
    detail: str


class PreflightReport(BaseModel):
    """What the outside world looks like just before we ask a CA for a cert."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    checks: tuple[PreflightCheck, ...]

    @property
    def blocking_failures(self) -> tuple[PreflightCheck, ...]:
        return tuple(
            check
            for check in self.checks
            if not check.passed and check.severity is PreflightSeverity.BLOCKING
        )

    @property
    def advisories(self) -> tuple[PreflightCheck, ...]:
        return tuple(
            check
            for check in self.checks
            if not check.passed and check.severity is PreflightSeverity.ADVISORY
        )

    @property
    def ok(self) -> bool:
        """True when nothing blocks issuance. Advisories do not count."""
        return not self.blocking_failures

    def summary(self) -> str:
        return "; ".join(
            f"{check.name}: {check.detail}" for check in self.blocking_failures
        )
