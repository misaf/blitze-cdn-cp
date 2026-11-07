"""Download GeoLite2 once, on the controller, for the whole fleet.

Edges never talk to MaxMind and never hold its license key: a release pins the
database the controller holds by path and digest, and every edge copies that
file like any other release material.
"""

from __future__ import annotations

from blitzecdn.capabilities.security.geoip.domain import (
    DATABASE_SHA256_VARIABLE,
    GeoIpRefresh,
)
from blitzecdn.capabilities.security.geoip.ports import (
    DatabaseSource,
    DatabaseStore,
    DeploymentLock,
    DeploymentRequirement,
    ReleaseHistory,
)


class GeoIpService:
    def __init__(
        self,
        *,
        store: DatabaseStore,
        source: DatabaseSource | None,
        lock: DeploymentLock,
        require_deployment: DeploymentRequirement,
        releases: ReleaseHistory,
    ) -> None:
        self._store = store
        self._source = source
        self._lock = lock
        self._require_deployment = require_deployment
        self._releases = releases

    @property
    def configured(self) -> bool:
        """Whether this controller has MaxMind credentials to refresh with."""
        return self._source is not None

    def refresh(self, operator: str = "scheduler") -> GeoIpRefresh:
        """Fetch the current database and, if it changed, have the fleet receive it.

        One download per controller however many edges there are, which is what
        keeps a fleet inside MaxMind's per-account daily limit. Without
        credentials this does nothing: the operator places databases by hand.
        """
        del operator  # Recorded by the job queue; nothing here is per-operator.
        if self._source is None:
            current = self._store.current()
            return GeoIpRefresh(
                configured=False,
                changed=False,
                sha256=None if current is None else current.sha256,
            )
        # The network, outside the lock: a slow MaxMind must not hold up a deploy.
        payload = self._source.download()
        # Inside it, so no release compiles between choosing the current file and
        # collecting the ones nothing names any more.
        with self._lock.lock():
            previous = self._store.current()
            database = self._store.install(payload)
            changed = previous is None or previous.sha256 != database.sha256
            if changed:
                self._require_deployment()
            removed = self._store.collect(
                frozenset({database.sha256}) | self._released_digests()
            )
        return GeoIpRefresh(
            configured=True,
            changed=changed,
            sha256=database.sha256,
            removed=removed,
        )

    def _released_digests(self) -> frozenset[str]:
        """Every database a retained release pins, so a rollback can still ship it."""
        return frozenset(
            digest
            for release in self._releases.list_releases(limit=None)
            for artifact in release.artifacts
            if isinstance(
                digest := artifact.document.get(DATABASE_SHA256_VARIABLE), str
            )
        )
