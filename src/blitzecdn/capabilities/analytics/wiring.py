"""Compose per-site analytics from the derived sites and stored traffic."""

from __future__ import annotations

from blitzecdn.capabilities.analytics.ports import StoredTraffic
from blitzecdn.capabilities.analytics.service import AnalyticsService
from blitzecdn.capabilities.dns.ports import SiteReader
from blitzecdn.core.runtime.resources import distribution_version

#: The installed distribution's version, read from package metadata.
__version__ = distribution_version(__name__)


def build_analytics_service(
    *, sites: SiteReader, traffic: StoredTraffic
) -> AnalyticsService:
    """Read-only: analytics never collects, writes or contacts an edge."""
    return AnalyticsService(sites=sites, traffic=traffic)


__all__ = ["__version__", "build_analytics_service"]
