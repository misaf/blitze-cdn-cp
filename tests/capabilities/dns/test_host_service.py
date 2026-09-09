"""Host writeback stays with the zone or rule that produced the host."""

import pytest
from control_plane_fixtures import FakeRunner, seed_site

from blitzecdn.capabilities.dns.domain import DomainPatch
from blitzecdn.capabilities.dns.service import HostService
from blitzecdn.capabilities.tls.policy import CertificateMode, SslMode
from blitzecdn.composition import ControlPlane
from blitzecdn.core.exceptions import NotFoundError


@pytest.fixture
def control(settings):
    platform = ControlPlane(settings=settings, runner=FakeRunner())
    try:
        yield platform
    finally:
        platform.close()


def test_host_reads_follow_canonical_edits(control):
    assert isinstance(control.sites, HostService)
    assert control.sites is control.site_editor
    assert not hasattr(control.dns, "list_sites")
    site = seed_site(control)
    control.dns.update_domain("example.com", DomainPatch(cache_enabled=False), "alice")
    assert control.sites.get_site(site.name).cache_enabled is False
    control.dns.delete_domain("example.com", "alice")
    assert control.sites.list_sites() == []
    with pytest.raises(NotFoundError):
        control.sites.get_site(site.name)


@pytest.mark.parametrize("rule_owned", [False, True])
def test_certificate_writeback_changes_only_the_host_source(control, rule_owned):
    original = seed_site(control)
    target = (
        seed_site(control, name="rule-host", record="api") if rule_owned else original
    )
    updated = control.site_editor.activate_managed_certificate(
        target, CertificateMode.REQUESTED
    )
    assert updated.certificate_mode is CertificateMode.REQUESTED
    assert updated.certificate_path.endswith(f"/{target.name}/fullchain.pem")
    if rule_owned:
        assert control.sites.get_site(original.name) == original
        assert (
            control.rules.get_rule("example.com", "api").overrides["certificate_mode"]
            == CertificateMode.REQUESTED
        )
    else:
        assert (
            control.dns.get_domain("example.com").certificate_mode
            is CertificateMode.REQUESTED
        )


def test_automatic_ssl_writeback_is_rule_scoped_and_monotonic(control):
    original = seed_site(control)
    target = seed_site(control, name="rule-host", record="api")
    control.site_editor.activate_managed_certificate(target, CertificateMode.REQUESTED)
    updated = control.site_editor.apply_automatic_ssl_upgrade(
        target.name, SslMode.FULL, "scheduler"
    )
    assert updated.ssl_mode is SslMode.FULL
    assert control.sites.get_site(original.name) == original
    assert (
        control.site_editor.apply_automatic_ssl_upgrade(
            target.name, SslMode.FLEXIBLE, "scheduler"
        )
        is None
    )
