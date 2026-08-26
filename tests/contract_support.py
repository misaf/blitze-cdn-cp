"""Shared paths and loaders for Ansible contract tests."""

# ruff: noqa: F401 -- these names are deliberately re-exported to test modules

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from blitzecdn.control_plane import ControlPlane
from blitzecdn.domain.dns import DnsRecord, Domain
from blitzecdn.domain.sites import (
    CacheQueryStringMode,
    CdnSite,
    CertificateMode,
    MinimumTlsVersion,
    SiteFirewall,
    SitePolicy,
    SslAutomaticMode,
    SslMode,
)
from blitzecdn.infrastructure.ansible_mapping import site_to_ansible
from blitzecdn.infrastructure.database import Repository

jinja2 = pytest.importorskip("jinja2")

PROJECT_DIR = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures/desired-state.yml"


#: The roles ship with this control plane, so there is no install step to get
#: wrong and no reason for these tests to skip. That matters: they used to read
#: an installed collection and skipped silently when it was absent, which turned
#: a broken contract into a green run.
ROLES_DIR = PROJECT_DIR / "ansible/roles"


def _role(name: str) -> Path:
    candidate = ROLES_DIR / name
    assert candidate.is_dir(), f"{name} is missing from ansible/roles/"
    return candidate


ROLE_DIR = _role("blitzecdn_nginx")


class _IndentedDumper(yaml.SafeDumper):
    """Indent sequences under their key, which is what yamllint expects."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def _role_spec() -> dict[str, Any]:
    document = yaml.safe_load(
        (ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )
    return document["argument_specs"]["main"]["options"]


def _role_defaults() -> dict[str, Any]:
    return yaml.safe_load((ROLE_DIR / "defaults/main.yml").read_text(encoding="utf-8"))


CACHE_ROLE_DIR = _role("blitzecdn_cache")
STATS_ROLE_DIR = _role("blitzecdn_stats")


@pytest.fixture
def desired_state(settings, tmp_path) -> dict[str, Any]:
    repository = Repository(settings.database_path)
    control = ControlPlane(settings=settings, repository=repository)
    repository.zones.create_domain(Domain(name="example.com"))
    repository.zones.create_record(
        DnsRecord.model_validate(
            {
                "domain": "example.com",
                "name": "cdn",
                "value": "198.51.100.20",
                "proxied": True,
                "ssl_mode": SslMode.OFF,
                "origin_request_host": "origin.example.com",
                "origin_sni": "origin.example.com",
                "cache_enabled": True,
                "cache_valid_success": "10m",
                "cache_valid_not_found": "1m",
            }
        )
    )
    repository.zones.create_record(
        DnsRecord.model_validate(
            {
                "domain": "example.com",
                "name": "static",
                "value": "192.0.2.10",
                "proxied": True,
                "ssl_mode": SslMode.FLEXIBLE,
                "enabled": False,
                "cache_enabled": False,
                "certificate_mode": CertificateMode.EXISTING,
                "certificate_path": "/etc/ssl/plain/fullchain.pem",
                "certificate_key_path": "/etc/ssl/plain/privkey.pem",
                "firewall": {
                    "allow_sources": ["203.0.113.9"],
                    "deny_sources": ["203.0.113.0/24", "2001:db8::/32"],
                    "denied_methods": ["DELETE", "TRACE"],
                    "denied_paths": ["/admin", "/.git"],
                },
            }
        )
    )
    control.deployments.write_desired_state(
        repository.snapshot(), settings.generated_vars_path
    )
    return yaml.safe_load(settings.generated_vars_path.read_text(encoding="utf-8"))


__all__ = [name for name in globals() if not name.startswith("__")]
