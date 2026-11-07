"""Fixtures for the edge-contract modules.

``desired_state`` renders the document a real deployment would converge.
Fixtures must be registered rather than imported, so it lives here.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from support.control_plane import build_application
from support.edge_render import seed_site

from blitzecdn.capabilities.dns.domain import Domain
from blitzecdn.capabilities.tls.contract import CertificateMode, SslMode
from blitzecdn.composition import Repository


@pytest.fixture
def desired_state(settings, tmp_path) -> dict[str, Any]:
    """Render desired state the way a real deployment would.

    Through the stores, proving zones, records and sites render into what the
    edge roles read.
    """
    repository = Repository(settings.database_path)
    control = build_application(settings=settings, repository=repository)
    repository.zones.create_domain(Domain(name="example.com"))
    seed_site(
        repository,
        name="example-com",
        label="cdn",
        # A proxied A record's value is the origin; the origin hostname travels in
        # origin_request_host and origin_sni.
        origin="198.51.100.20",
        **{
            "ssl_mode": SslMode.OFF,
            "origin_request_host": "origin.example.com",
            "origin_sni": "origin.example.com",
            "cache_enabled": True,
            "cache_valid_success": "10m",
            "cache_valid_not_found": "1m",
        },
    )
    seed_site(
        repository,
        name="static-example-com",
        label="static",
        origin="192.0.2.10",
        **{
            "ssl_mode": SslMode.FLEXIBLE,
            "enabled": False,
            "cache_enabled": False,
            "certificate_mode": CertificateMode.EXISTING,
            "certificate_path": "/etc/ssl/plain/fullchain.pem",
            "certificate_key_path": "/etc/ssl/plain/privkey.pem",
            # No country rules: CI feeds this fixture to a real playbook with GeoIP off,
            # where the role refuses them. Country rendering is covered against the
            # template.
            "firewall": {
                "allow_sources": ["203.0.113.9"],
                "deny_sources": ["203.0.113.0/24", "2001:db8::/32"],
                "denied_methods": ["DELETE", "TRACE"],
                "denied_paths": ["/admin", "/.git"],
            },
        },
    )
    control.deployments.publish_artifact(
        control.releases.compile(), settings.generated_vars_path
    )
    return yaml.safe_load(settings.generated_vars_path.read_text(encoding="utf-8"))
