"""What is checked before a certificate is asked for.

A report about the world outside: whether the hostname resolves to this fleet,
whether the record's TTL makes a cutover risky, whether CAA permits our CA. It
names no certificate type on purpose — nothing here has seen one yet, and the
issuance that follows is `service/issuance.py`'s.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

__all__ = [
    "TTL_CUTOVER_ADVISORY_SECONDS",
    "PreflightCheck",
    "PreflightReport",
    "PreflightSeverity",
]


#: A proxied record with a TTL above this is flagged before issuance.
#:
#: Not an error — a long TTL is legitimate for a hostname that has been live
#: for months. It matters only around a cutover: resolvers that cached the old
#: address keep answering with it for up to the TTL, and HTTP-01 validation
#: follows public DNS, so a slow rollover shows up as an issuance failure with
#: no obvious cause.
TTL_CUTOVER_ADVISORY_SECONDS = 3600


class PreflightSeverity(StrEnum):
    """Whether a failed check stops issuance or is merely reported.

    ``BLOCKING`` is reserved for conditions under which HTTP-01 validation
    cannot succeed, so failing early costs nothing and spares the CA's rate
    limit. Everything a certificate can legitimately be issued in spite of —
    an origin that is down, a TTL that is high — is ``ADVISORY``.
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
