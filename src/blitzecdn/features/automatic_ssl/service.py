"""Cloudflare-style Automatic SSL/TLS recommendation and upgrade."""

from __future__ import annotations

from collections.abc import Mapping

from blitzecdn.features.automatic_ssl.domain import SslAutomaticReconciliation
from blitzecdn.features.deployments.ports import DeploymentGateway, DeploymentRunner
from blitzecdn.features.dns.ports import SiteStore, ZoneEditor
from blitzecdn.features.dns.site_domain import (
    CdnSite,
    CertificateMode,
    SslAutomaticMode,
    SslMode,
)
from blitzecdn.features.edges.origins import OriginCheck, OriginReport
from blitzecdn.features.edges.ports import OriginProbe
from blitzecdn.features.edges.reporting import edge_origins


class AutomaticSslService:
    """Probe both origin transports and apply only proven upgrades.

    Cloudflare's scanner compares HTTP and HTTPS representations and rolls an
    upgrade out gradually. BlitzeCDN cannot split a single Nginx virtual host by
    traffic percentage, so it takes the conservative equivalent available to
    this architecture: every edge must answer, HTTP and HTTPS must return the
    same status on each edge, and one fleet-wide deploy applies the upgrade.
    Any missing or inconsistent answer leaves the current mode untouched.
    """

    def __init__(
        self,
        *,
        sites: SiteStore,
        runner: DeploymentRunner,
        origin_probe: OriginProbe,
        dns: ZoneEditor,
        deployments: DeploymentGateway,
    ) -> None:
        self.sites = sites
        self.runner = runner
        self.origin_probe = origin_probe
        self.dns = dns
        self.deployments = deployments

    def reconcile(self, operator: str) -> SslAutomaticReconciliation:
        candidates = [
            site
            for site in self.sites.list_sites()
            if site.enabled
            and site.ssl_automatic_mode is SslAutomaticMode.AUTO
            and site.certificate_mode is not CertificateMode.DISABLED
            and site.ssl_mode is not SslMode.FULL_STRICT
        ]
        scanned = tuple(site.name for site in candidates)
        if not candidates:
            return SslAutomaticReconciliation(scanned=scanned)

        current = self._probe(candidates)
        strict = self._probe(
            [
                site.model_copy(update={"ssl_mode": SslMode.FULL_STRICT})
                for site in candidates
            ]
        )
        upgraded: dict[str, SslMode] = {}
        skipped: dict[str, str] = {}

        fleet_problem = self._fleet_problem(current, strict)
        if fleet_problem is not None:
            return SslAutomaticReconciliation(
                scanned=scanned,
                skipped={site.name: fleet_problem for site in candidates},
            )

        current_checks = _checks_by_site(current)
        strict_checks = _checks_by_site(strict)
        for site in candidates:
            recommendation, reason = _recommend(
                site.ssl_mode,
                current_checks.get(site.name, {}),
                strict_checks.get(site.name, {}),
            )
            if recommendation is None:
                skipped[site.name] = reason
                continue
            applied = self.dns.apply_automatic_ssl_upgrade(
                site.name, recommendation, operator
            )
            if applied is None:
                skipped[site.name] = "policy changed while the scan was running"
                continue
            upgraded[site.name] = applied.ssl_mode

        deployment = self.deployments.deploy(operator) if upgraded else None
        return SslAutomaticReconciliation(
            scanned=scanned,
            upgraded=upgraded,
            skipped=skipped,
            deployment=deployment,
        )

    def _probe(self, sites: list[CdnSite]) -> OriginReport:
        rendered = [
            {**self.origin_probe.to_probe(site), "compare_content": True}
            for site in sites
        ]
        run = self.runner.run_origin_check(sites=rendered)
        return OriginReport(
            checked_at=run.finished_at,
            edges=tuple(edge_origins(host) for host in run.hosts),
        )

    @staticmethod
    def _fleet_problem(current: OriginReport, strict: OriginReport) -> str | None:
        if not current.edges or not strict.edges:
            return "the edge fleet returned no origin report"
        silent = sorted({edge.host for edge in (*current.silent, *strict.silent)})
        if silent:
            return f"origin scan incomplete on edges: {', '.join(silent)}"
        current_hosts = {edge.host for edge in current.reporting}
        strict_hosts = {edge.host for edge in strict.reporting}
        if current_hosts != strict_hosts:
            return "the HTTP and HTTPS scans were answered by different edges"
        return None


def _checks_by_site(report: OriginReport) -> dict[str, dict[str, OriginCheck]]:
    grouped: dict[str, dict[str, OriginCheck]] = {}
    for edge in report.reporting:
        for check in edge.checks:
            grouped.setdefault(check.site, {})[edge.host] = check
    return grouped


def _recommend(
    current_mode: SslMode,
    current: Mapping[str, OriginCheck],
    strict: Mapping[str, OriginCheck],
) -> tuple[SslMode | None, str]:
    if not current or current.keys() != strict.keys():
        return None, "one or more edges omitted this site from the scan"
    if any(
        not check.ok or (check.status is not None and check.status >= 500)
        for check in current.values()
    ):
        return None, "the current origin transport is not healthy on every edge"

    consistent = all(
        (current[edge].status, current[edge].content_sha256)
        == (strict[edge].status, strict[edge].content_sha256)
        for edge in current
    )
    compared_content = all(
        check.content_sha256 is not None
        for check in (*current.values(), *strict.values())
    )
    if consistent and compared_content and all(check.ok for check in strict.values()):
        target = SslMode.FULL_STRICT
    elif (
        consistent
        and compared_content
        and all(check.reachable for check in strict.values())
    ):
        target = SslMode.FULL
    elif current_mode is SslMode.OFF:
        target = SslMode.FLEXIBLE
    else:
        return None, "HTTPS was unavailable or differed from HTTP"

    if target.security_rank <= current_mode.security_rank:
        return None, "no stronger compatible mode was found"
    return target, ""
