"""Zone policy, the rules that override it, and what a hostname resolves to."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from support.control_plane import FakeRunner, build_application, seed_site
from support.policy import TEST_CERTIFICATE_VERSION

from blitzecdn.capabilities.compression.contract import CompressionMode
from blitzecdn.capabilities.dns.domain import (
    Domain,
    DomainPatch,
    PatchPolicy,
    Rule,
    RulePatch,
    resolve_policy,
)
from blitzecdn.capabilities.dns.service import DnsService, RuleService
from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.capabilities.tls.contract import CertificateMode, SslMode
from blitzecdn.composition import Repository
from blitzecdn.core.exceptions import ConflictError, NotFoundError


def _control(settings, repository):
    return build_application(
        settings=settings, repository=repository, runner=FakeRunner()
    )


# -- The zone carries the policy ------------------------------------------


def test_a_zone_is_delegable_before_anything_is_served_from_it():
    """No origin, no TLS, no decisions. Adding a domain is one fact.

    Each proxied hostname names its own origin on its record.
    """
    zone = Domain(name="example.com")
    assert zone.policy.cache.cache_enabled
    assert not zone.policy.tls.ssl_mode.serves_tls


def test_a_zone_refuses_what_a_site_refused(settings):
    """The cross-field rules hold on the zone.

    HTTP/3 without edge TLS is the pair worth pinning: nginx accepts it and no
    visitor can use it.
    """
    with pytest.raises(ValueError, match="http3_enabled=True requires ssl_mode"):
        Domain(
            name="example.com", policy=SitePolicy.from_settings({"http3_enabled": True})
        )


def test_the_zone_policy_is_stored_and_read_back_whole(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(
        Domain(
            name="example.com",
            policy=SitePolicy.from_settings(
                {"cache_valid_success": "1h", "compression": "gzip"}
            ),
        ),
        "tester",
    )

    stored = repository.zones.get_domain("example.com")
    assert stored.policy.cache.cache_valid_success == "1h"
    assert stored.policy.compression.compression.value == "gzip"


def test_a_zone_patch_merges_rather_than_replaces(settings):
    """Half a pair is what a patch validated alone would let through."""
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(
        Domain(
            name="example.com",
            policy=SitePolicy.from_settings({"cache_valid_success": "1h"}),
        ),
        "tester",
    )

    updated = control.dns.update_domain(
        "example.com",
        PatchPolicy.from_patch(DomainPatch(cache_enabled=False)),
        "tester",
    )

    assert not updated.policy.cache.cache_enabled
    assert updated.policy.cache.cache_valid_success == "1h"


def test_a_zone_patch_that_would_break_a_pair_is_refused(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")

    with pytest.raises(ValueError, match="http3_enabled=True requires ssl_mode"):
        control.dns.update_domain(
            "example.com",
            PatchPolicy.from_patch(DomainPatch(http3_enabled=True)),
            "tester",
        )


def test_a_zone_patch_cannot_claim_a_controller_managed_certificate(settings):
    """The mode belongs to the issuer; an operator setting it de-serves the zone.

    `CdnSite` refuses managed paths not derived from that host's name, and
    `derive_hosts` drops such a host. A zone derives several hosts, so only the
    entry point knows who is asking.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")

    with pytest.raises(ValueError, match="set by the certificate upload"):
        control.dns.update_domain(
            "example.com",
            PatchPolicy.from_patch(
                DomainPatch(
                    ssl_mode=SslMode.FULL,
                    certificate_mode=CertificateMode.REQUESTED,
                    certificate_path="/etc/blitzecdn/tls/example.com/fullchain.pem",
                    certificate_key_path="/etc/blitzecdn/tls/example.com/privkey.pem",
                )
            ),
            "tester",
        )


def test_an_operator_may_still_point_a_zone_at_material_of_their_own(settings):
    """`existing` is the operator's mode, and the guard must not take it away."""
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")

    zone = control.dns.update_domain(
        "example.com",
        PatchPolicy.from_patch(
            DomainPatch(
                ssl_mode=SslMode.FULL,
                certificate_mode=CertificateMode.EXISTING,
                certificate_path="/etc/blitzecdn/tls/example.com/fullchain.pem",
                certificate_key_path="/etc/blitzecdn/tls/example.com/privkey.pem",
            )
        ),
        "tester",
    )
    assert zone.policy.tls.certificate_mode is CertificateMode.EXISTING


# -- A rule is an override, not a policy -----------------------------------


def test_a_rule_cannot_claim_a_controller_managed_certificate(settings):
    """A rule cannot author what `update_domain` refuses.

    Asked by the service, not by `Rule`: `_validate_overrides` runs on every
    rehydrated rule, and the issuer's own rules legitimately carry the mode.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")

    with pytest.raises(ValueError, match="set by the certificate upload"):
        control.rules.create_rule(
            Rule(
                domain="example.com",
                name="r",
                match="*.example.com",
                overrides={"certificate_mode": CertificateMode.UPLOADED},
            ),
            "tester",
        )


def test_a_rule_the_issuer_wrote_still_reads_back(settings):
    """A rule the issuer wrote still reads back.

    `HostService.activate_managed_certificate` records an issued certificate as a
    rule override, so the store legitimately holds that mode.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")
    control.rules.create_rule(
        Rule(
            domain="example.com",
            name="r",
            match="*.example.com",
            overrides={"cache_enabled": False},
        ),
        "tester",
    )
    stored = repository.rules.get_rule("example.com", "r")
    repository.rules.replace_rule(
        stored.model_copy(
            update={
                "overrides": {
                    **stored.overrides,
                    "certificate_mode": CertificateMode.UPLOADED,
                }
            }
        )
    )

    read_back = repository.rules.list_rules("example.com")
    assert read_back[0].overrides["certificate_mode"] == CertificateMode.UPLOADED


def test_a_rule_can_only_override_settings_a_zone_has():
    with pytest.raises(ValueError, match="can only override zone settings"):
        Rule(domain="example.com", name="r", overrides={"cache_enabldd": False})


def test_a_rule_cannot_override_the_zones_identity():
    with pytest.raises(ValueError, match="can only override zone settings"):
        Rule(domain="example.com", name="r", overrides={"name": "other.com"})


def test_a_rule_that_overrides_nothing_is_refused():
    """It would still match, and shadow every rule behind it, changing nothing."""
    with pytest.raises(ValueError, match="at least one setting"):
        Rule(domain="example.com", name="r", overrides={})


def test_a_rule_value_is_checked_against_what_the_setting_takes():
    with pytest.raises(ValueError):
        Rule(domain="example.com", name="r", overrides={"cache_enabled": "maybe"})


def test_a_rule_cannot_reach_a_hostname_in_another_zone():
    with pytest.raises(ValueError, match="is not inside"):
        Rule(
            domain="example.com",
            name="r",
            match="api.other.net",
            overrides={"cache_enabled": False},
        )


@pytest.mark.parametrize(
    ("match", "fqdn", "covered"),
    [
        ("*", "example.com", True),
        ("*", "api.example.com", True),
        ("*", "other.net", False),
        ("api.example.com", "api.example.com", True),
        ("api.example.com", "www.example.com", False),
        ("*.static.example.com", "a.static.example.com", True),
        # nginx reads `*.name` the same way: the wildcard stands for at least
        # one label, so the bare name is not covered by its own wildcard.
        ("*.static.example.com", "static.example.com", False),
    ],
)
def test_what_a_match_covers(match, fqdn, covered):
    rule = Rule(
        domain="example.com", name="r", match=match, overrides={"cache_enabled": False}
    )
    assert rule.matches(fqdn) is covered


def test_a_disabled_rule_covers_nothing():
    """Off is off. The alternative is a rule that still shadows the ones after it."""
    rule = Rule(
        domain="example.com",
        name="r",
        match="*",
        enabled=False,
        overrides={"cache_enabled": False},
    )
    assert not rule.matches("api.example.com")


# -- Resolution ------------------------------------------------------------


def test_a_hostname_with_no_rule_is_served_by_its_zone():
    zone = Domain(
        name="example.com",
        policy=SitePolicy.from_settings({"cache_valid_success": "1h"}),
    )
    resolved = resolve_policy(zone, [], "www.example.com")
    assert resolved.rule is None
    assert resolved.policy.policy.cache.cache_valid_success == "1h"


def test_the_first_matching_rule_wins_and_the_rest_are_not_merged():
    """Two rules match; the answer comes from one of them, whole.

    Priority decides, and the result says which rule it was.
    """
    zone = Domain(
        name="example.com",
        policy=SitePolicy.from_settings({"cache_valid_success": "10m"}),
    )
    rules = [
        Rule(
            domain="example.com",
            name="wide",
            priority=50,
            match="*",
            overrides={"cache_valid_success": "1h"},
        ),
        Rule(
            domain="example.com",
            name="narrow",
            priority=10,
            match="api.example.com",
            overrides={"cache_enabled": False},
        ),
    ]

    resolved = resolve_policy(zone, rules, "api.example.com")

    assert resolved.rule == "narrow"
    assert not resolved.policy.policy.cache.cache_enabled
    # `wide` matched too and did not apply: its 1h is absent, and the zone's
    # own value is what stands.
    assert resolved.policy.policy.cache.cache_valid_success == "10m"


def test_resolution_does_not_depend_on_the_order_it_is_handed():
    """First match wins is about priority, not about list order."""
    zone = Domain(name="example.com")
    first = Rule(
        domain="example.com",
        name="a",
        priority=10,
        match="*",
        overrides={"cache_valid_success": "1h"},
    )
    second = Rule(
        domain="example.com",
        name="b",
        priority=20,
        match="*",
        overrides={"cache_valid_success": "5m"},
    )

    assert resolve_policy(zone, [first, second], "x.example.com").rule == "a"
    assert resolve_policy(zone, [second, first], "x.example.com").rule == "a"


def test_a_rule_that_merges_into_an_impossible_zone_is_refused():
    """The pair rules are the zone's, and a rule cannot get around them."""
    zone = Domain(name="example.com")
    rule = Rule(
        domain="example.com", name="r", match="*", overrides={"http3_enabled": True}
    )

    with pytest.raises(ValueError, match="http3_enabled=True requires ssl_mode"):
        resolve_policy(zone, [rule], "www.example.com")


# -- The editor ------------------------------------------------------------


def test_rules_are_stored_ordered_and_resolved_end_to_end(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")
    control.rules.create_rule(
        Rule(
            domain="example.com",
            name="wide",
            priority=50,
            match="*",
            overrides={"cache_valid_success": "1h"},
        ),
        "tester",
    )
    control.rules.create_rule(
        Rule(
            domain="example.com",
            name="api",
            priority=10,
            match="api.example.com",
            overrides={"cache_enabled": False},
        ),
        "tester",
    )

    assert [rule.name for rule in control.rules.list_rules("example.com")] == [
        "api",
        "wide",
    ]
    assert control.rules.resolve("example.com", "api.example.com").rule == "api"
    assert control.rules.resolve("example.com", "www.example.com").rule == "wide"


def test_a_rule_for_a_zone_we_do_not_serve_is_refused_by_name(settings):
    """Named, rather than surfacing as the foreign key's integrity error."""
    repository = Repository(settings.database_path)
    control = _control(settings, repository)

    with pytest.raises(NotFoundError, match=r"example\.com"):
        control.rules.create_rule(
            Rule(domain="example.com", name="r", overrides={"cache_enabled": False}),
            "tester",
        )


def test_two_rules_cannot_share_a_name_in_one_zone(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")
    rule = Rule(domain="example.com", name="r", overrides={"cache_enabled": False})
    control.rules.create_rule(rule, "tester")

    with pytest.raises(ConflictError, match="already exists"):
        control.rules.create_rule(rule, "tester")


def test_a_patch_replaces_the_overrides_rather_than_merging_them(settings):
    """Otherwise a rule could never stop overriding a setting it once did."""
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")
    control.rules.create_rule(
        Rule(
            domain="example.com",
            name="r",
            overrides={"cache_enabled": False, "cache_valid_success": "1h"},
        ),
        "tester",
    )

    updated = control.rules.update_rule(
        "example.com", "r", RulePatch(overrides={"cache_valid_success": "5m"}), "tester"
    )

    assert dict(updated.overrides) == {"cache_valid_success": "5m"}


#: Overrides each acceptable to `Rule` one setting at a time, and refused by the
#: zone they land on: a contract's field validator, and a cross-contract rule.
_UNSERVABLE_OVERRIDES = pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"certificate_path": "relative/cert.pem"}, "must be absolute"),
        ({"http3_enabled": True}, "http3_enabled=True requires ssl_mode"),
    ],
    ids=["contract-field-validator", "cross-contract-rule"],
)


@_UNSERVABLE_OVERRIDES
def test_a_rule_its_zone_could_not_serve_is_refused_on_create(
    settings, overrides, reason
):
    """Stored, it would drop every hostname it matches out of `derive_hosts`."""
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")

    with pytest.raises(ValueError, match=reason):
        control.rules.create_rule(
            Rule(domain="example.com", name="r", overrides=overrides), "tester"
        )

    assert control.rules.list_rules("example.com") == []


@_UNSERVABLE_OVERRIDES
def test_a_rule_its_zone_could_not_serve_is_refused_on_patch(
    settings, overrides, reason
):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")
    original = control.rules.create_rule(
        Rule(domain="example.com", name="r", overrides={"cache_enabled": False}),
        "tester",
    )

    with pytest.raises(ValueError, match=reason):
        control.rules.update_rule(
            "example.com", "r", RulePatch(overrides=overrides), "tester"
        )

    assert control.rules.get_rule("example.com", "r") == original


def test_a_disabled_rule_is_still_checked_against_its_zone(settings):
    """Switching it on later would otherwise be the edit that breaks the hosts."""
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")

    with pytest.raises(ValueError, match="http3_enabled=True requires ssl_mode"):
        control.rules.create_rule(
            Rule(
                domain="example.com",
                name="r",
                overrides={"http3_enabled": True},
                enabled=False,
            ),
            "tester",
        )


def _zone_serving_tls(control):
    control.dns.create_domain(Domain(name="example.com"), "tester")
    control.dns.update_domain(
        "example.com",
        PatchPolicy.from_patch(
            DomainPatch(
                ssl_mode=SslMode.FULL,
                certificate_mode=CertificateMode.EXISTING,
                certificate_path="/etc/blitzecdn/tls/example.com/fullchain.pem",
                certificate_key_path="/etc/blitzecdn/tls/example.com/privkey.pem",
            )
        ),
        "tester",
    )


def test_a_rule_the_zone_can_serve_is_stored(settings):
    """The same override is fine where the zone supplies what it needs."""
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    _zone_serving_tls(control)

    control.rules.create_rule(
        Rule(domain="example.com", name="r", overrides={"http3_enabled": True}),
        "tester",
    )

    resolved = control.rules.resolve("example.com", "www.example.com")
    assert resolved.policy.policy.protocols.http3_enabled


def test_a_zone_edit_that_would_break_a_stored_rule_is_refused(settings):
    """Refused, naming the rule, rather than left for `blitzecdn validate` to find.

    Accepted, the rule's hostnames would leave the derived host list on the next
    deploy with no error at the edit that caused it.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    _zone_serving_tls(control)
    control.rules.create_rule(
        Rule(domain="example.com", name="h3", overrides={"http3_enabled": True}),
        "tester",
    )
    before = control.dns.get_domain("example.com")

    with pytest.raises(ConflictError, match=r"rule 'h3'.*http3_enabled=True requires"):
        control.dns.update_domain(
            "example.com",
            PatchPolicy.from_patch(DomainPatch(ssl_mode=SslMode.OFF)),
            "tester",
        )

    assert control.dns.get_domain("example.com") == before


def test_deleting_a_zone_takes_its_rules_with_it(settings):
    """They are overrides on a policy that no longer exists."""
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")
    control.rules.create_rule(
        Rule(domain="example.com", name="r", overrides={"cache_enabled": False}),
        "tester",
    )

    control.dns.delete_domain("example.com", "tester")

    assert repository.rules.list_rules() == []


def test_a_rollback_restores_the_rules_the_release_was_compiled_from(settings):
    """Rules cascade with their zone, so release inputs must carry them.

    ``adopt_inputs`` rewrites the zone rows and rules are keyed ON DELETE CASCADE;
    without them in the inputs, adoption would delete every rule.
    """
    from blitzecdn.capabilities.deployments.service import rollback as rollback_policy

    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")
    control.rules.create_rule(
        Rule(domain="example.com", name="api", overrides={"cache_enabled": False}),
        "tester",
    )
    inputs = repository.release_inputs()
    assert [rule.name for rule in inputs.rules] == ["api"]

    # Something changes, and then the older state is adopted back.
    control.rules.delete_rule("example.com", "api", "tester")
    assert repository.rules.list_rules() == []

    with repository.transaction():
        rollback_policy.adopt_inputs(
            repository.zones, repository.rules, repository.ansible_settings, inputs
        )

    assert [rule.name for rule in repository.rules.list_rules()] == ["api"]


def test_a_rollback_restores_the_settings_the_release_was_compiled_from(settings):
    """The release carries the fleet settings its edges were converged with."""
    from blitzecdn.capabilities.deployments.service import rollback as rollback_policy

    repository = Repository(settings.database_path)
    _control(settings, repository)
    repository.ansible_settings.set_setting("blitzecdn_nginx_hsts_enabled", True)
    inputs = repository.release_inputs()
    assert inputs.settings == {"blitzecdn_nginx_hsts_enabled": True}

    repository.ansible_settings.delete_setting("blitzecdn_nginx_hsts_enabled")
    repository.ansible_settings.set_setting("blitzecdn_nginx_ssl_stapling", True)

    with repository.transaction():
        rollback_policy.adopt_inputs(
            repository.zones, repository.rules, repository.ansible_settings, inputs
        )

    assert repository.ansible_settings.list_settings() == {
        "blitzecdn_nginx_hsts_enabled": True
    }


def _competing_writer(control, write):
    """A Unit of Work that commits somebody else's write as the lock is taken.

    The window is after this caller read and before it writes; this asserts the
    read happens inside `BEGIN IMMEDIATE`.
    """

    class _Interfering:
        @contextmanager
        def transaction(self):
            with control.transactions.transaction():
                write()
                yield

    return _Interfering()


def test_two_operators_patching_one_zone_do_not_lose_a_setting(settings):
    """A zone patch is a whole document, so a stale read is a silent revert.

    Reading inside the boundary builds the write from the document it replaces.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "alice")

    editor = DnsService(
        zones=repository.zones,
        rules=repository.rules,
        events=control.events,
        uow=_competing_writer(
            control,
            lambda: control.dns.update_domain(
                "example.com",
                PatchPolicy.from_patch(DomainPatch(cache_enabled=False)),
                "bob",
            ),
        ),
    )
    editor.update_domain(
        "example.com",
        PatchPolicy.from_patch(DomainPatch(compression=CompressionMode.GZIP)),
        "alice",
    )

    zone = control.dns.get_domain("example.com")
    assert zone.policy.compression.compression is CompressionMode.GZIP
    assert zone.policy.cache.cache_enabled is False


def test_an_operator_editing_a_rule_cannot_revert_the_issuers_certificate(settings):
    """``overrides`` is replaced wholesale, so a stale read drops what it lacks.

    An operator changing only ``match`` must not drop a certificate the issuer
    wrote in between.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    seed_site(control, name="rule-host", record="api")

    site = control.sites.get_site("example-com--api")
    editor = RuleService(
        rules=repository.rules,
        zones=repository.zones,
        events=control.events,
        uow=_competing_writer(
            control,
            lambda: control.site_editor.activate_managed_certificate(
                site, CertificateMode.REQUESTED, TEST_CERTIFICATE_VERSION
            ),
        ),
    )
    editor.update_rule("example.com", "api", RulePatch(match="*.example.com"), "alice")

    rule = control.rules.get_rule("example.com", "api")
    assert rule.match == "*.example.com"
    assert rule.overrides["certificate_mode"] == CertificateMode.REQUESTED


def test_the_zone_and_rule_stores_refuse_a_write_built_on_a_stale_read(settings):
    """The compare-and-swap under the two editors, asked directly.

    `expected` refuses a stale document however it was assembled.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    # `cache_enabled` makes the update a real change: a patch to the current value
    # moves nothing.
    seed_site(control, name="rule-host", record="api", cache_enabled=True)

    stale_zone = control.dns.get_domain("example.com")
    control.dns.update_domain(
        "example.com", PatchPolicy.from_patch(DomainPatch(cache_enabled=False)), "bob"
    )
    with (
        repository.transaction(),
        pytest.raises(ConflictError, match="changed while it was being edited"),
    ):
        repository.zones.replace_domain(stale_zone, expected=stale_zone)

    stale_rule = control.rules.get_rule("example.com", "api")
    control.rules.update_rule("example.com", "api", RulePatch(priority=50), "bob")
    with (
        repository.transaction(),
        pytest.raises(ConflictError, match="changed while it was being edited"),
    ):
        repository.rules.replace_rule(stale_rule, expected=stale_rule)


def test_a_compare_and_swap_outside_a_unit_of_work_is_refused(settings):
    """Outside `BEGIN IMMEDIATE` the comparison and the update are not one act.

    SQLite would answer with a snapshot-busy error instead of the conflict, so
    the store refuses up front.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    zone = Domain(name="a.test")
    control.dns.create_domain(zone, "alice")

    with pytest.raises(ValueError, match="must run inside a Unit of Work"):
        repository.zones.replace_domain(zone, expected=zone)
