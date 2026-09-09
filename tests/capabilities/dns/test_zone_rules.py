"""Zone policy, the rules that override it, and what a hostname resolves to."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from control_plane_fixtures import FakeRunner, seed_site

from blitzecdn.capabilities.compression.policy import CompressionMode
from blitzecdn.capabilities.dns.domain import (
    Domain,
    DomainPatch,
    Rule,
    RulePatch,
    resolve_policy,
)
from blitzecdn.capabilities.dns.service import DnsService, RuleService
from blitzecdn.capabilities.tls.policy import CertificateMode, SslMode
from blitzecdn.composition import ControlPlane, Repository
from blitzecdn.core.exceptions import ConflictError, NotFoundError


def _control(settings, repository):
    return ControlPlane(settings=settings, repository=repository, runner=FakeRunner())


# -- The zone carries the policy ------------------------------------------


def test_a_zone_is_delegable_before_anything_is_served_from_it():
    """No origin, no TLS, no decisions. Adding a domain is one fact.

    An origin need not be chosen here because it does not live here: each
    proxied hostname names its own origin on its record. A placeholder in the
    field that decides where traffic goes would be worse than an absence,
    because an absence can be refused later — and this zone has no decision
    to make until something is proxied.
    """
    zone = Domain(name="example.com")
    assert zone.cache_enabled
    assert not zone.ssl_mode.serves_tls


def test_a_zone_refuses_what_a_site_refused(settings):
    """The cross-field rules moved with the policy and still hold.

    HTTP/3 without edge TLS is the one worth pinning: it is the pair that
    reaches an edge as a config nginx accepts and no visitor can use.
    """
    with pytest.raises(ValueError, match="http3_enabled=True requires ssl_mode"):
        Domain(name="example.com", http3_enabled=True)


def test_the_zone_policy_is_stored_and_read_back_whole(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(
        Domain(
            name="example.com",
            cache_valid_success="1h",
            compression="gzip",
        ),
        "tester",
    )

    stored = repository.zones.get_domain("example.com")
    assert stored.cache_valid_success == "1h"
    assert stored.compression.value == "gzip"


def test_a_zone_patch_merges_rather_than_replaces(settings):
    """Half a pair is what a patch validated alone would let through."""
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(
        Domain(name="example.com", cache_valid_success="1h"), "tester"
    )

    updated = control.dns.update_domain(
        "example.com", DomainPatch(cache_enabled=False), "tester"
    )

    assert not updated.cache_enabled
    assert updated.cache_valid_success == "1h"


def test_a_zone_patch_that_would_break_a_pair_is_refused(settings):
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")

    with pytest.raises(ValueError, match="http3_enabled=True requires ssl_mode"):
        control.dns.update_domain(
            "example.com", DomainPatch(http3_enabled=True), "tester"
        )


def test_a_zone_patch_cannot_claim_a_controller_managed_certificate(settings):
    """The mode belongs to the issuer, and an operator taking it de-serves the zone.

    Not a validation nicety. `CdnSite` refuses a controller-managed mode whose
    paths are not the ones derived from *that host's* name, `derive_hosts`
    drops a host it cannot build rather than raising, and the result is a zone
    whose hostnames quietly stop being served — visible only to
    `blitzecdn validate`, which nobody runs before the traffic goes.

    The zone itself cannot catch it: one zone derives several hosts, its own
    and one per rule, so it cannot know which name the paths should have been
    built from. Only the entry point knows who is asking.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")

    with pytest.raises(ValueError, match="set by the certificate upload"):
        control.dns.update_domain(
            "example.com",
            DomainPatch(
                ssl_mode=SslMode.FULL,
                certificate_mode=CertificateMode.REQUESTED,
                certificate_path="/etc/blitzecdn/tls/example.com/fullchain.pem",
                certificate_key_path="/etc/blitzecdn/tls/example.com/privkey.pem",
            ),
            "tester",
        )


def test_an_operator_may_still_point_a_zone_at_material_of_their_own(settings):
    """`existing` is the operator's mode, and the guard must not take it away.

    Material somebody else put on the box, which the control plane neither
    issues nor renews. It is the whole reason the guard names modes rather than
    forbidding the three certificate fields outright.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")

    zone = control.dns.update_domain(
        "example.com",
        DomainPatch(
            ssl_mode=SslMode.FULL,
            certificate_mode=CertificateMode.EXISTING,
            certificate_path="/etc/blitzecdn/tls/example.com/fullchain.pem",
            certificate_key_path="/etc/blitzecdn/tls/example.com/privkey.pem",
        ),
        "tester",
    )
    assert zone.certificate_mode is CertificateMode.EXISTING


# -- A rule is an override, not a policy -----------------------------------


def test_a_rule_cannot_claim_a_controller_managed_certificate(settings):
    """The other door into the same policy, and it was standing open too.

    `_NOT_OVERRIDABLE` held only the zone's identity, so a rule could author
    what `update_domain` now refuses.

    Asked by the service rather than by `Rule`, and the difference is not
    cosmetic: `_validate_overrides` runs on every rule the store rehydrates,
    so refusing the mode there would make the issuer's own rules — which
    legitimately carry it — unreadable, and `list_rules` would raise for the
    whole fleet.
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
    """The regression the first placement of that guard caused, held down.

    `HostService.activate_managed_certificate` records an issued certificate
    as a rule override, so a controller-managed mode is a value the store
    legitimately holds. Refusing it on the way *out* took down every read.
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
    zone = Domain(name="example.com", cache_valid_success="1h")
    resolved = resolve_policy(zone, [], "www.example.com")
    assert resolved.rule is None
    assert resolved.policy.cache_valid_success == "1h"


def test_the_first_matching_rule_wins_and_the_rest_are_not_merged():
    """Two rules match; the answer comes from one of them, whole.

    Merging both would make the effective policy an intersection an operator
    has to assemble in their head before they can predict a change. Priority
    decides, and the result says which rule it was.
    """
    zone = Domain(name="example.com", cache_valid_success="10m")
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
    assert not resolved.policy.cache_enabled
    # `wide` matched too and did not apply: its 1h is absent, and the zone's
    # own value is what stands.
    assert resolved.policy.cache_valid_success == "10m"


def test_resolution_does_not_depend_on_the_order_it_is_handed():
    """First match wins is about priority, not about list order.

    The store returns them ordered, and this says a caller that assembled the
    list some other way cannot get a different answer by accident.
    """
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
    """The pair rules are the zone's, and a rule cannot get around them.

    HTTP/3 on a zone that serves no TLS is refused when the zone is written.
    A rule turning it on for one hostname is the same broken pair arriving by
    a different door, and it is refused at the same place.
    """
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


def test_a_rollback_restores_the_rules_it_snapshotted(settings):
    """The sharp edge of putting rules in a zone: they cascade with it.

    ``adopt_snapshot`` deletes the zone rows and writes them again, and a rule
    is keyed to its zone with ON DELETE CASCADE. A snapshot that did not carry
    rules would not merely fail to restore them — the adoption would delete
    every rule in the installation on its way past.
    """
    from blitzecdn.capabilities.deployments.domain.snapshots import (
        decode_snapshot_state,
    )
    from blitzecdn.capabilities.deployments.service import rollback as rollback_policy

    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    control.dns.create_domain(Domain(name="example.com"), "tester")
    control.rules.create_rule(
        Rule(domain="example.com", name="api", overrides={"cache_enabled": False}),
        "tester",
    )
    snapshot = repository.snapshot()
    assert [rule.name for rule in decode_snapshot_state(snapshot)[2]] == ["api"]

    # Something changes, and then the older snapshot is adopted back.
    control.rules.delete_rule("example.com", "api", "tester")
    assert repository.rules.list_rules() == []

    with repository.transaction():
        rollback_policy.adopt_snapshot(repository.zones, repository.rules, snapshot)

    assert [rule.name for rule in repository.rules.list_rules()] == ["api"]


def _competing_writer(control, write):
    """A Unit of Work that commits somebody else's write as the lock is taken.

    Places a competing commit in the one window a read-modify-write has: after
    this caller read, before it writes. The real boundary closes that window by
    doing both under `BEGIN IMMEDIATE`, so what this asserts is that the read
    happens inside the transaction — a property a threaded race can only
    demonstrate on a good day.
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

    Both operators change one setting each. Merged onto a zone read before the
    other landed, the second write carries the *old* value of the first one's
    field alongside its own, and neither operator is told that a setting went
    back. Reading inside the boundary is what makes the merge a merge: the
    write is built from the document the write will replace, not from one that
    stopped existing while the request was in flight.

    Records were already written this way and zones were not, which left the
    aggregate every hostname inherits from as the unprotected one.
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
                "example.com", DomainPatch(cache_enabled=False), "bob"
            ),
        ),
    )
    editor.update_domain(
        "example.com", DomainPatch(compression=CompressionMode.GZIP), "alice"
    )

    zone = control.dns.get_domain("example.com")
    assert zone.compression is CompressionMode.GZIP
    assert zone.cache_enabled is False


def test_an_operator_editing_a_rule_cannot_revert_the_issuers_certificate(settings):
    """``overrides`` is replaced wholesale, so a stale read drops what it lacks.

    The issuer writes a certificate into a rule's overrides unattended. An
    operator who read that rule beforehand and then changes only its ``match``
    writes the whole mapping back — without the certificate — and the rule's
    hostnames lose the material they were just issued, with nothing in the
    audit trail saying so.
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
                site, CertificateMode.REQUESTED
            ),
        ),
    )
    editor.update_rule("example.com", "api", RulePatch(match="*.example.com"), "alice")

    rule = control.rules.get_rule("example.com", "api")
    assert rule.match == "*.example.com"
    assert rule.overrides["certificate_mode"] == CertificateMode.REQUESTED


def test_the_zone_and_rule_stores_refuse_a_write_built_on_a_stale_read(settings):
    """The compare-and-swap under the two editors, asked directly.

    Reading inside the Unit of Work is what keeps a caller from assembling a
    stale document; `expected` is what refuses one that got assembled anyway —
    by a caller outside this package, or by a future edit that moves a read
    back out. The second is the reason it is a check and not a convention:
    conventions do not fail the build.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    seed_site(control, name="rule-host", record="api")

    stale_zone = control.dns.get_domain("example.com")
    control.dns.update_domain("example.com", DomainPatch(cache_enabled=False), "bob")
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

    SQLite answers the upgrade from a deferred read to a write with a
    snapshot-busy error no `busy_timeout` waits out, which reaches the caller
    as something other than the conflict it is. The store says so up front
    rather than leaving it to a docstring, which is the same call the record
    store already makes.
    """
    repository = Repository(settings.database_path)
    control = _control(settings, repository)
    zone = Domain(name="a.test")
    control.dns.create_domain(zone, "alice")

    with pytest.raises(ValueError, match="must run inside a Unit of Work"):
        repository.zones.replace_domain(zone, expected=zone)
