"""What a prepared release renders to, and that it is kept as it was rendered.

The controller is the only renderer of edge configuration. These hold that
preparation records the rendering, that the record is the rendering, and the
properties of the tree each kind of configuration produces.
"""

from __future__ import annotations

from pathlib import Path

from support.certificates import certificate_control_plane
from support.control_plane import (
    build_application,
    seed_site,
    with_capability_settings,
)

from blitzecdn.capabilities.dns.domain import DomainPatch, PatchPolicy
from blitzecdn.composition import ApplicationServices, Repository
from blitzecdn.core.domain.configuration import ConfigurationBundle

_SECRET = "parity-test-signing-key-" + "x" * 32


def _assert_parity(control: ApplicationServices, tmp_path: Path) -> ConfigurationBundle:
    """Prepare a release; its stored configuration is its rendering."""
    release = control.releases.prepare()
    assert release.servable, release.findings
    stored = control.releases.configuration(release.id)

    assert stored == control.releases.renderer.render(release.execution_document())
    assert ConfigurationBundle.decode(stored.encode()) == stored
    assert release.digest in stored.files["nginx.conf"].decode()
    return stored


def test_a_plain_and_a_disabled_tls_site_are_rendered_and_kept(settings, tmp_path):
    repository = Repository(settings.database_path)
    control = build_application(settings=settings, repository=repository)
    seed_site(control, origin_request_host="origin.example.com", ssl_mode="off")
    seed_site(
        control,
        name="static-example-com",
        record="static",
        enabled=False,
        cache_enabled=False,
        ssl_mode="flexible",
        certificate_mode="existing",
        certificate_path="/etc/ssl/plain/fullchain.pem",
        certificate_key_path="/etc/ssl/plain/privkey.pem",
        firewall={
            "allow_sources": ["203.0.113.9"],
            "deny_sources": ["203.0.113.0/24", "2001:db8::/32"],
            "denied_methods": ["DELETE", "TRACE"],
            "denied_paths": ["/admin", "/.git"],
        },
    )

    bundle = _assert_parity(control, tmp_path)

    disabled = [
        site.name for site in control.sites.list_sites() if not site.policy.enabled
    ]
    assert disabled
    for name in disabled:
        assert f"nginx/sites-available/{name}.conf" in bundle.files
        assert f"nginx/sites-enabled/{name}.conf" not in bundle.links


def test_an_uploaded_certificate_is_rendered_and_kept(
    settings, tmp_path, certificate_pair
):
    control = certificate_control_plane(settings)
    site = seed_site(control)
    certificate, key = certificate_pair()
    control.certificates.upload_certificate(site.name, certificate, key, "alice")
    control.dns.update_domain(
        "example.com",
        PatchPolicy.from_patch(DomainPatch(ssl_mode="full", always_use_https=True)),
        "alice",
    )

    bundle = _assert_parity(control, tmp_path)

    rendered = bundle.files["nginx/sites-available/example-com.conf"].decode()
    assert "ssl_certificate " in rendered


def test_fleet_settings_and_under_attack_are_rendered_and_kept(settings, tmp_path):
    configured = with_capability_settings(settings, under_attack_secret=_SECRET)
    repository = Repository(configured.database_path)
    control = build_application(settings=configured, repository=repository)
    for name, value in {
        "blitzecdn_security_under_attack_enabled": True,
        "blitzecdn_nginx_hsts_enabled": True,
        "blitzecdn_nginx_ssl_stapling": True,
        "blitzecdn_nginx_proxy_read_timeout": "45s",
        "blitzecdn_nginx_default_server": False,
    }.items():
        repository.ansible_settings.set_setting(name, value)
    seed_site(control, under_attack_mode=True, http3_enabled=False)
    seed_site(
        control,
        name="example-org",
        domain="example.org",
        record="api",
        ssl_mode="flexible",
        certificate_mode="existing",
        certificate_path="/etc/ssl/plain/fullchain.pem",
        certificate_key_path="/etc/ssl/plain/privkey.pem",
        http3_enabled=True,
    )

    bundle = _assert_parity(control, tmp_path)

    module = bundle.files["njs/under-attack.js"].decode()
    assert _SECRET not in module
    assert all(_SECRET not in content.decode() for content in bundle.files.values())
    assert "nginx/sites-available/blitzecdn-default.conf" not in bundle.files
