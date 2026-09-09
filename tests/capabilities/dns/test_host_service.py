"""Host writeback stays with the zone or rule that produced the host."""

from contextlib import contextmanager

import pytest
from control_plane_fixtures import FakeRunner, seed_record, seed_site
from pydantic import ValidationError

from blitzecdn.capabilities.dns.domain import DomainPatch, Rule
from blitzecdn.capabilities.dns.service import HostService
from blitzecdn.capabilities.tls.policy import (
    CertificateMode,
    SslAutomaticMode,
    SslMode,
)
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


def _bypass_the_editor(control, **changes):
    """Write a zone straight to the store, the way a rollback or restore does.

    The entry-point guards live on the services. A snapshot adopted wholesale,
    a restored backup, and the certificates capability's own writeback all
    reach `replace_domain` without passing them, so this is the state the
    derivation actually has to survive — not a state an operator can type.
    """
    zone = control.dns.get_domain("example.com")
    # `model_copy` runs no validators, which is the point: it produces the row
    # a restore writes, not one the model would accept.
    control.dns.zones.replace_domain(zone.model_copy(update=changes))


def test_a_zone_the_derivation_refuses_drops_its_host_and_not_the_fleet(control):
    """One inconsistent zone darkens its own hostnames, and nobody else's.

    `CdnSite` refuses a controller-managed mode whose paths were derived from
    some other host's name — which is exactly what a rollback leaves behind
    when it restores a zone without the rule that named the paths. Raising
    would take `list_sites` with it: the fleet is derived in one pass, and the
    API reports a `ValidationError` as a 422, so a corrupt stored row would
    reach an operator as though their own request were malformed.
    """
    seed_site(control)
    seed_site(control, domain="other.test", record="cdn", name="other-test")
    _bypass_the_editor(
        control,
        certificate_mode=CertificateMode.REQUESTED,
        certificate_path="/etc/blitzecdn/tls/somebody-else/fullchain.pem",
        certificate_key_path="/etc/blitzecdn/tls/somebody-else/privkey.pem",
    )

    served = control.sites.list_sites()
    assert [site.name for site in served] == ["other-test"]
    with pytest.raises(NotFoundError):
        control.sites.get_site("example-com")


def test_the_refused_host_is_named_by_validation_rather_than_left_silent(control):
    """A drop nobody can see is worse than the raise it replaced.

    `validate` runs before a deploy converges anything, and this is where the
    hostnames that stopped being served are said out loud.
    """
    seed_site(control)
    _bypass_the_editor(
        control,
        certificate_mode=CertificateMode.REQUESTED,
        certificate_path="/etc/blitzecdn/tls/somebody-else/fullchain.pem",
        certificate_key_path="/etc/blitzecdn/tls/somebody-else/privkey.pem",
    )

    (error,) = control.dns.validation_errors()
    assert "cdn.example.com" in error
    assert "example-com" in error
    assert "cannot be served" in error
    # The model's own words, not pydantic's frame around them.
    assert "certificate_mode='requested' is set by the certificate upload" in error
    assert "validation error" not in error


def test_a_rule_that_contradicts_its_zone_darkens_only_its_own_hostnames(control):
    """The other failure point: resolution, before any group has a name.

    A rule's overrides are validated field by field — by handing them to
    `DomainPatch`, which asks nothing across two of them — so a combination
    that only contradicts the *zone* is storable through the ordinary rule
    editor, and is refused the first time the two are merged. This is that
    state surviving: the rule's hostname goes dark and is named, the zone's
    own hostnames keep serving.
    """
    seed_site(control)
    seed_record(control, name="api")
    control.rules.create_rule(
        Rule(
            domain="example.com",
            name="api",
            match="api.example.com",
            overrides={"http3_enabled": True},
        ),
        "alice",
    )

    assert [site.name for site in control.sites.list_sites()] == ["example-com"]
    (error,) = control.dns.validation_errors()
    assert error.startswith("api.example.com cannot be served")
    assert "http3_enabled=True requires ssl_mode" in error


class _InterferingUnitOfWork:
    """A Unit of Work that lets one operator write land as the lock is taken.

    A stand-in for the race, not a simulation of SQLite: what it reproduces is
    the one ordering that matters — a competing write committed after the scan
    formed its opinion and before this transaction writes. The real boundary
    keeps that window shut by reading inside `BEGIN IMMEDIATE`; this asserts
    the code reads there at all, which is the part a race cannot be trusted to
    demonstrate on a schedule.
    """

    def __init__(self, inner, on_enter):
        self._inner = inner
        self._on_enter = on_enter

    @contextmanager
    def transaction(self):
        with self._inner.transaction():
            self._on_enter()
            yield


def test_an_operator_opting_out_mid_scan_beats_the_upgrade(control):
    """The guard is asked inside the transaction, so the later write wins.

    `SslAutomaticReconciliation` probes origins over the network before it
    proposes anything, so its opinion is always somewhat stale. An operator who
    leaves Auto during that window must not have a stronger mode written onto
    their zone afterwards — and the only place that can be decided is under the
    same lock as the write.
    """
    site = seed_site(control)
    control.site_editor.activate_managed_certificate(site, CertificateMode.REQUESTED)
    site = control.sites.get_site(site.name)
    assert site.ssl_automatic_mode is not SslAutomaticMode.CUSTOM

    def opt_out():
        control.dns.zones.replace_domain(
            control.dns.get_domain("example.com").model_copy(
                update={"ssl_automatic_mode": SslAutomaticMode.CUSTOM}
            )
        )

    editor = HostService(
        zones=control.dns.zones,
        rules=control.rules.rules,
        events=control.events,
        uow=_InterferingUnitOfWork(control.transactions, opt_out),
    )
    assert editor.apply_automatic_ssl_upgrade(site.name, SslMode.FULL, "scan") is None
    assert control.sites.get_site(site.name).ssl_mode is site.ssl_mode


def test_rule_writeback_validates_what_it_stores(control):
    """The rule branch is checked the way the zone branch always was.

    Called directly because there is no caller that can reach it with a bad
    value — which is the whole reason the missing validation survived. What is
    under test is the guarantee the method makes, not the values today's two
    callers happen to send.
    """
    site = seed_site(control, name="rule-host", record="api")
    with pytest.raises(ValidationError):
        control.site_editor._apply_to_source(site, {"certificate_mode": "nonsense"})
    assert (
        "certificate_mode" not in control.rules.get_rule("example.com", "api").overrides
    )
