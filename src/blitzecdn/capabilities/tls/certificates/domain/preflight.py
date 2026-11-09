"""What is checked about the outside world before a certificate is asked for."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PREFLIGHT_BACKOFF_MAX_SECONDS",
    "TTL_CUTOVER_ADVISORY_SECONDS",
    "PreflightAttempt",
    "PreflightBackoff",
    "PreflightCheck",
    "PreflightReport",
    "PreflightSeverity",
]


#: A proxied record with a TTL above this is flagged: around a cutover,
#: resolvers keep the old address for the TTL, and HTTP-01 follows public DNS.
TTL_CUTOVER_ADVISORY_SECONDS = 3600

#: However often reconciliation runs, a site whose preflight keeps blocking is
#: checked at least once a day.
PREFLIGHT_BACKOFF_MAX_SECONDS = 86_400


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


class PreflightAttempt(BaseModel):
    """The last blocked preflight of a site waiting for its first certificate.

    Kept so scheduled reconciliation can back off a site that is never pointed at
    the edges, and so ``overview`` can say when it was last looked at.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: str
    #: Digest of the site and its DNS record when checked; any change to either
    #: makes the site due again at once.
    fingerprint: str
    #: Consecutive blocked checks with this fingerprint.
    failures: int = Field(ge=1)
    checked_at: datetime
    next_check_at: datetime
    summary: str


class PreflightBackoff(BaseModel):
    """How long scheduled reconciliation leaves a blocked site alone.

    The wait doubles with each consecutive blocked check, starting at one
    reconciliation interval and capped at a day.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    interval_seconds: int = Field(ge=1)

    def delay(self, failures: int) -> timedelta:
        # Past 2**17 intervals of one second the cap has long applied.
        exponent = min(max(failures, 1) - 1, 17)
        seconds = min(
            self.interval_seconds * 2**exponent, PREFLIGHT_BACKOFF_MAX_SECONDS
        )
        return timedelta(seconds=seconds)

    def after(
        self,
        previous: PreflightAttempt | None,
        *,
        site: str,
        fingerprint: str,
        checked_at: datetime,
        summary: str,
    ) -> PreflightAttempt:
        """The attempt recorded for another blocked check of ``site``."""
        failures = (
            previous.failures + 1
            if previous is not None and previous.fingerprint == fingerprint
            else 1
        )
        return PreflightAttempt(
            site=site,
            fingerprint=fingerprint,
            failures=failures,
            checked_at=checked_at,
            next_check_at=checked_at + self.delay(failures),
            summary=summary,
        )

    def due(
        self, attempt: PreflightAttempt | None, *, fingerprint: str, now: datetime
    ) -> bool:
        """Whether a scheduled run should check the site again.

        Half an interval of slack absorbs a run that starts, or reaches the
        site, a little earlier than the one that recorded the attempt.
        """
        if attempt is None or attempt.fingerprint != fingerprint:
            return True
        slack = timedelta(seconds=self.interval_seconds / 2)
        return now + slack >= attempt.next_check_at


class ChallengeEdges(BaseModel):
    """Which edges an HTTP-01 token for one hostname has to be on.

    The CA fetches the token from an address the hostname resolves to, so an edge
    it does not resolve to can be down without failing validation. ``skipped``
    names those edges, so an issuance can say which ones it did not wait for.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hostname: str
    #: Edges the token is placed on. Empty means the whole fleet: the hostname
    #: points at no edge we can see, and only the CA knows which one it will ask.
    targets: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
