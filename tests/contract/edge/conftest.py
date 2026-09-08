"""Fixtures for the edge-contract modules.

``desired_state`` renders the document a real deployment would converge, and
several modules assert against it. A fixture has to be registered with pytest
rather than imported, so it lives here rather than in
``edge_render_support.py`` beside the loaders it uses.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from edge_render_support import _seed_site

from blitzecdn.capabilities.dns.domain import Domain
from blitzecdn.capabilities.tls.policy import CertificateMode, SslMode
from blitzecdn.composition import ControlPlane, Repository


@pytest.fixture
def desired_state(settings, tmp_path) -> dict[str, Any]:
    """Render desired state the way a real deployment would.

    Through the stores rather than by hand-writing a document: the snapshot
    carries the zones, the records and the sites, and this fixture is what
    proves the three of them render into what the edge roles read.
    """
    repository = Repository(settings.database_path)
    control = ControlPlane(settings=settings, repository=repository)
    repository.zones.create_domain(Domain(name="example.com"))
    _seed_site(
        repository,
        name="example-com",
        label="cdn",
        # An A record has no address of its own once it routes to a site, so the
        # origin lives here — and the origin *hostname* travels in
        # origin_request_host and origin_sni beside it.
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
    _seed_site(
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
            # No country rules here on purpose. CI feeds this fixture to a
            # real playbook, where blitzecdn_nginx_geoip_enabled is false
            # and the role is supposed to refuse them. Country rendering is
            # covered against the template directly, below.
            "firewall": {
                "allow_sources": ["203.0.113.9"],
                "deny_sources": ["203.0.113.0/24", "2001:db8::/32"],
                "denied_methods": ["DELETE", "TRACE"],
                "denied_paths": ["/admin", "/.git"],
            },
        },
    )
    control.deployments.write_desired_state(
        repository.snapshot(), settings.generated_vars_path
    )
    return yaml.safe_load(settings.generated_vars_path.read_text(encoding="utf-8"))
