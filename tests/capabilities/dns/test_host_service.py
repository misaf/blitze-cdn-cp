"""Host writeback stays with the zone or rule that produced the host."""

from contextlib import contextmanager

import pytest
from pydantic import ValidationError
from support.control_plane import FakeRunner, build_application, seed_record, seed_site
from support.policy import TEST_CERTIFICATE_VERSION

from blitzecdn.capabilities.cache.contract import CachePolicy
from blitzecdn.capabilities.dns.domain import DomainPatch, PatchPolicy, Rule
from blitzecdn.capabilities.dns.service import HostService
from blitzecdn.capabilities.tls.certificates.service import SiteEditing
from blitzecdn.capabilities.tls.contract import (
    CertificateMode,
    SslAutomaticMode,
    SslMode,
    TlsPolicy,
)
from blitzecdn.core.exceptions import NotFoundError


@pytest.fixture
def control(settings):
    platform = build_application(settings=settings, runner=FakeRunner())
    try:
        yield platform
    finally:
        platform.close()


def test_host_reads_follow_canonical_edits(control):
    assert isinstance(control.sites, HostService)
    assert control.sites is control.hosts
    assert not hasattr(control.dns, "list_sites")
    site = seed_site(control)
    control.dns.update_domain(
        "example.com", PatchPolicy.from_patch(DomainPatch(cache_enabled=False)), "alice"
    )
    assert control.sites.get_site(site.name).policy[CachePolicy].cache_enabled is False
    control.dns.delete_domain("example.com", "alice")
    assert control.sites.list_sites() == []
    with pytest.raises(NotFoundError):
        control.sites.get_site(site.name)


def test_host_reads_share_one_snapshot_and_join_an_existing_write(control, monkeypatch):
    seed_site(control)
    database = control.transactions.database
    observed = []

    def observe(read):
        def wrapped():
            session = database.current_session()
            assert session is not None
            observed.append(session)
            return read()

        return wrapped

    for store, method in (
        (control.sites.zones, "list_domains"),
        (control.sites.rules, "list_rules"),
        (control.sites.zones, "list_records"),
    ):
        monkeypatch.setattr(store, method, observe(getattr(store, method)))

    assert control.sites.list_sites()
    assert len(observed) == 3
    assert all(session is observed[0] for session in observed)
    assert database.current_session() is None

    observed.clear()
    with control.transactions.transaction():
        session = database.current_session()
        assert control.sites.get_site("example-com")
        assert all(value is session for value in observed)


def test_host_read_failure_releases_the_transaction(control, monkeypatch):
    def broken():
        raise RuntimeError("read interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(control.sites.rules, "list_rules", broken)
        with pytest.raises(RuntimeError, match="read interrupted"):
            control.sites.list_sites()

    assert control.transactions.database.current_session() is None
    assert seed_site(control).name == "example-com"


@pytest.mark.parametrize("rule_owned", [False, True])
def test_certificate_writeback_changes_only_the_host_source(control, rule_owned):
    original = seed_site(control)
    target = (
        seed_site(control, name="rule-host", record="api") if rule_owned else original
    )
    updated = control.site_editor.activate_managed_certificate(
        target, CertificateMode.REQUESTED, TEST_CERTIFICATE_VERSION
    )
    assert updated.policy[TlsPolicy].certificate_mode is CertificateMode.REQUESTED
    assert updated.policy[TlsPolicy].certificate_path.endswith(
        f"/{target.name}/fullchain-{TEST_CERTIFICATE_VERSION.certificate_digest}.pem"
    )
    if rule_owned:
        assert control.sites.get_site(original.name) == original
        assert (
            control.rules.get_rule("example.com", "api").overrides["certificate_mode"]
            == CertificateMode.REQUESTED
        )
    else:
        assert (
            control.dns.get_domain("example.com").policy[TlsPolicy].certificate_mode
            is CertificateMode.REQUESTED
        )


def test_automatic_ssl_writeback_is_rule_scoped_and_monotonic(control):
    original = seed_site(control)
    target = seed_site(control, name="rule-host", record="api")
    control.site_editor.activate_managed_certificate(
        target, CertificateMode.REQUESTED, TEST_CERTIFICATE_VERSION
    )
    updated = control.site_editor.apply_automatic_ssl_upgrade(
        target.name, SslMode.FULL, "scheduler"
    )
    assert updated.policy[TlsPolicy].ssl_mode is SslMode.FULL
    assert control.sites.get_site(original.name) == original
    assert (
        control.site_editor.apply_automatic_ssl_upgrade(
            target.name, SslMode.FLEXIBLE, "scheduler"
        )
        is None
    )


def _bypass_the_editor(control, **changes):
    """Write a zone straight to the store, the way a rollback or restore does.

    Snapshots, restored backups and the certificate writeback reach
    `replace_domain` without the service guards, so the derivation must survive
    this state.
    """
    zone = control.dns.get_domain("example.com")
    # `model_copy` runs no validators, which is the point: it produces the row
    # a restore writes, not one the model would accept.
    control.dns.zones.replace_domain(
        zone.model_copy(
            update={
                "policy": zone.policy.model_copy(
                    update={"tls": zone.policy[TlsPolicy].model_copy(update=changes)}
                )
            }
        )
    )


def test_a_zone_the_derivation_refuses_drops_its_host_and_not_the_fleet(control):
    """One inconsistent zone darkens its own hostnames, and nobody else's.

    A rollback can restore a zone without the rule that named its managed
    certificate paths, which `CdnSite` refuses. Raising would fail `list_sites` for
    the whole fleet, reported as a 422.
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
    """A dropped host is named by `validate`, which runs before a deploy."""
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

    The editors refuse such a rule, and a zone edit that would make one, but
    state also arrives by restore and by rollback, so it is written to the store
    here, past `RuleService`. The rule's hostname goes dark and is named; the
    zone's keep serving.
    """
    seed_site(control)
    seed_record(control, name="api")
    control.rules.rules.create_rule(
        Rule(
            domain="example.com",
            name="api",
            match="api.example.com",
            overrides={"http3_enabled": True},
        )
    )

    assert [site.name for site in control.sites.list_sites()] == ["example-com"]
    (error,) = control.dns.validation_errors()
    assert error.startswith("api.example.com cannot be served")
    assert "http3_enabled=True requires ssl_mode" in error


class _InterferingUnitOfWork:
    """A Unit of Work that lets one operator write land as the lock is taken.

    Reproduces the ordering that matters: a competing write after the scan formed
    its opinion and before this transaction writes. This asserts the code reads
    inside the transaction.
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

    `SslAutomaticReconciliation` probes over the network first, so its opinion is
    stale; an operator leaving Auto meanwhile must not get a stronger mode.
    """
    site = seed_site(control)
    control.site_editor.activate_managed_certificate(
        site, CertificateMode.REQUESTED, TEST_CERTIFICATE_VERSION
    )
    site = control.sites.get_site(site.name)
    assert site.policy[TlsPolicy].ssl_automatic_mode is not SslAutomaticMode.CUSTOM

    def opt_out():
        zone = control.dns.get_domain("example.com")
        control.dns.zones.replace_domain(
            type(zone)(
                name=zone.name,
                policy=zone.policy.applying(
                    {"ssl_automatic_mode": SslAutomaticMode.CUSTOM}
                ),
            )
        )

    editor = SiteEditing(
        writer=HostService(
            zones=control.dns.zones,
            rules=control.rules.rules,
            events=control.events,
            uow=_InterferingUnitOfWork(control.transactions, opt_out),
        )
    )
    assert editor.apply_automatic_ssl_upgrade(site.name, SslMode.FULL, "scan") is None
    assert (
        control.sites.get_site(site.name).policy[TlsPolicy].ssl_mode
        is site.policy[TlsPolicy].ssl_mode
    )


def test_rule_writeback_validates_what_it_stores(control):
    """The rule branch is checked the way the zone branch always was.

    Called directly: no current caller can pass a bad value, and the guarantee is
    the method's.
    """
    site = seed_site(control, name="rule-host", record="api")
    with pytest.raises(ValidationError):
        control.hosts._apply_to_source(site, {"certificate_mode": "nonsense"})
    assert (
        "certificate_mode" not in control.rules.get_rule("example.com", "api").overrides
    )
