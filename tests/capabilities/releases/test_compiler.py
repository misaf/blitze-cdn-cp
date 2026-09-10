"""The compiler is a function of its arguments, and says so in its digests.

Nothing here starts a control plane, opens a database or registers a plugin.
That is the property under test as much as any assertion below: if compiling a
release ever needs one of those, these tests stop compiling and the boundary is
reported where it broke.
"""

from __future__ import annotations

import pytest

from blitzecdn.capabilities.dns.domain import CdnSite, DnsRecord, Domain, Rule
from blitzecdn.capabilities.releases.domain import (
    COMPILER_VERSION,
    DESIRED_STATE_ARTIFACT,
    EdgeCapabilities,
    ReleaseInputs,
    SettingOrigin,
)
from blitzecdn.capabilities.releases.service import compile_release


class StubContributors:
    """The share of an artifact one installation's capabilities contribute.

    Deliberately not a fake plugin registry. The compiler declared a port with
    two methods on it, and the double that stands in for it should be able to
    hold nothing but the answers — otherwise the test is exercising the
    registry's merge order rather than the compiler.
    """

    def __init__(self, fleet: dict[str, object] | None = None) -> None:
        self.fleet = fleet or {}

    def site_variables(self, site: CdnSite) -> dict[str, object]:
        return {
            "blitzecdn_site_name": site.name,
            "blitzecdn_site_server_names": list(site.server_names),
            "blitzecdn_site_origin": site.origin_host,
        }

    def fleet_variables(self, sites: tuple[CdnSite, ...]) -> dict[str, object]:
        return {**self.fleet, "blitzecdn_site_count": len(sites)}


def no_checks(site: CdnSite) -> tuple[()]:
    return ()


def inputs_for(
    *,
    zone: str = "example.com",
    rules: tuple[Rule, ...] = (),
    records: tuple[DnsRecord, ...] | None = None,
    **policy: object,
) -> ReleaseInputs:
    domain = Domain.model_validate({"name": zone, **policy})
    if records is None:
        records = (
            DnsRecord(domain=zone, name="cdn", value="198.51.100.10", proxied=True),
        )
    return ReleaseInputs.of([domain], list(records), list(rules))


def compile_for(inputs: ReleaseInputs, **overrides: object):
    # A zone with no settings on it already requests two capabilities: caching
    # and compression are both on by default. A baseline of nothing installed
    # would make every test in this file assert findings it is not about, so
    # the default here is "the wheels a default zone needs".
    arguments: dict[str, object] = {
        "capabilities": ("cache", "compression"),
        "targets": (),
        "contributors": StubContributors(),
        "site_checks": no_checks,
        "allow_empty_sites": False,
    }
    arguments.update(overrides)
    return compile_release(inputs, **arguments)  # type: ignore[arg-type]


# -- Reproducibility ------------------------------------------------------


def test_the_same_inputs_compile_to_the_same_digest():
    inputs = inputs_for()
    assert compile_for(inputs).digest == compile_for(inputs).digest


def test_equal_state_built_separately_compiles_to_the_same_digest():
    """Reproducible across processes, not merely across two calls on one object.

    Building the inputs twice is the point: a digest that depended on object
    identity, insertion order or a default factory would pass the test above
    and fail this one, and it is this one that models a controller restarting.
    """
    assert compile_for(inputs_for()).digest == compile_for(inputs_for()).digest


def test_record_order_does_not_change_the_artifact():
    ordered = (
        DnsRecord(domain="example.com", name="a", value="198.51.100.10"),
        DnsRecord(domain="example.com", name="b", value="198.51.100.10"),
    )
    forwards = compile_for(inputs_for(records=ordered))
    backwards = compile_for(inputs_for(records=tuple(reversed(ordered))))
    assert forwards.artifact(DESIRED_STATE_ARTIFACT).digest == (
        backwards.artifact(DESIRED_STATE_ARTIFACT).digest
    )


def test_changing_a_setting_changes_the_release_digest():
    before = compile_for(inputs_for())
    after = compile_for(inputs_for(cache_enabled=False))
    assert before.digest != after.digest
    assert before.inputs_digest != after.inputs_digest


def test_the_installed_capability_set_is_part_of_the_identity():
    """Detaching a wheel changes what the edges are served, so it changes the id.

    Without this the fleet could be converged from an artifact rendered by a
    capability that is no longer installed, with the release still looking
    current.
    """
    inputs = inputs_for()
    assert (
        compile_for(inputs).digest
        != compile_for(inputs, capabilities=("cache", "compression", "http3")).digest
    )


def test_the_targeted_edges_are_part_of_the_identity():
    inputs = inputs_for()
    aimed = compile_for(inputs, targets=(EdgeCapabilities(name="edge-01"),))
    assert compile_for(inputs).digest != aimed.digest


def test_the_compiler_version_is_recorded_on_the_release():
    assert compile_for(inputs_for()).compiler_version == COMPILER_VERSION


def test_the_release_id_is_the_short_digest():
    release = compile_for(inputs_for())
    assert release.id == release.digest[:16]
    assert len(release.id) == 16


# -- Artifacts ------------------------------------------------------------


def test_the_artifact_carries_every_servable_site_and_the_fleet_variables():
    release = compile_for(
        inputs_for(), contributors=StubContributors({"blitzecdn_fleet": "yes"})
    )
    document = release.artifact(DESIRED_STATE_ARTIFACT).document
    assert document["blitzecdn_fleet"] == "yes"
    assert document["blitzecdn_nginx_allow_empty_sites"] is False
    assert [
        site["blitzecdn_site_name"] for site in document["blitzecdn_nginx_sites"]
    ] == ["example-com"]


def test_a_zone_with_no_proxied_record_contributes_no_server_block():
    """Desired state a rollback must restore, and an instruction to no edge.

    A site with no hostnames would render a `server` block with an empty
    `server_name`, which nginx reads as the default server for the listener.
    """
    inputs = inputs_for(
        records=(
            DnsRecord(
                domain="example.com", name="cdn", value="198.51.100.10", proxied=False
            ),
        )
    )
    release = compile_for(inputs)
    assert release.sites == ()
    assert (
        release.artifact(DESIRED_STATE_ARTIFACT).document["blitzecdn_nginx_sites"] == []
    )


def test_an_unknown_artifact_is_refused_with_the_names_that_exist():
    with pytest.raises(KeyError, match=DESIRED_STATE_ARTIFACT):
        compile_for(inputs_for()).artifact("per-edge")


# -- Capability validation -------------------------------------------------


def test_a_site_requesting_an_uninstalled_capability_is_a_finding():
    release = compile_for(inputs_for(compression="gzip"), capabilities=("cache",))
    assert not release.servable
    finding = next(item for item in release.findings if item.source == "capabilities")
    assert "'compression' is not installed" in finding.message
    assert "compression" in finding.message
    assert finding.host == "example-com"


def test_the_capability_is_satisfied_once_the_distribution_is_installed():
    release = compile_for(
        inputs_for(compression="gzip"), capabilities=("cache", "compression")
    )
    assert release.servable


def test_an_edge_that_declares_nothing_is_assumed_to_match_the_controller():
    """The behaviour every fleet registered before edges could declare a set has.

    Making the per-edge check opt-in is what lets it be added without changing
    a single existing installation's outcome.
    """
    release = compile_for(
        inputs_for(compression="gzip"),
        capabilities=("cache", "compression"),
        targets=(EdgeCapabilities(name="edge-01"),),
    )
    assert release.servable


def test_an_edge_that_declares_a_set_without_the_capability_is_a_finding():
    release = compile_for(
        inputs_for(compression="gzip"),
        capabilities=("cache", "compression"),
        targets=(EdgeCapabilities(name="edge-01", capabilities=("cache", "http3")),),
    )
    assert not release.servable
    message = release.findings[0].message
    assert "edge 'edge-01' does not provide capability 'compression'" in message


def test_every_finding_is_reported_rather_than_the_first():
    """An operator fixing a fleet gets the whole list, not one hostname a run."""
    release = compile_for(
        inputs_for(
            compression="gzip",
            records=(
                DnsRecord(domain="example.com", name="a", value="198.51.100.10"),
                DnsRecord(domain="example.com", name="b", value="198.51.100.11"),
            ),
        ),
        capabilities=("cache",),
    )
    assert len({finding.host for finding in release.findings}) == 2


def test_a_capability_check_contributed_by_a_plugin_reaches_the_release():
    from blitzecdn.capabilities.releases.domain import ReleaseFinding

    def refuse(site: CdnSite) -> tuple[ReleaseFinding, ...]:
        return (ReleaseFinding(source="waf", host=site.name, message="rules unparsed"),)

    release = compile_for(inputs_for(), site_checks=refuse)
    assert [finding.source for finding in release.findings] == ["waf"]


# -- Explanations ----------------------------------------------------------


def test_a_zone_setting_is_attributed_to_the_zone():
    release = compile_for(inputs_for(cache_enabled=False))
    settings = {item.setting: item for item in release.explanations[0].settings}
    assert settings["cache_enabled"].origin is SettingOrigin.ZONE
    assert settings["cache_enabled"].value is False
    assert settings["cache_enabled"].rule is None


def test_a_setting_nobody_touched_is_attributed_to_the_schema_default():
    settings = {
        item.setting: item
        for item in compile_for(inputs_for()).explanations[0].settings
    }
    assert settings["cache_enabled"].origin is SettingOrigin.DEFAULT


def test_an_overridden_setting_names_the_rule_that_set_it():
    """The half `/resolve` could not answer: which value the rule actually chose.

    A rule that wins and overrides nothing relevant leaves the zone's value in
    place, and an operator told only "rule 'api' applied" goes and edits the
    rule.
    """
    rule = Rule(
        domain="example.com",
        name="api",
        match="api.example.com",
        priority=10,
        overrides={"cache_enabled": False},
    )
    inputs = inputs_for(
        rules=(rule,),
        records=(DnsRecord(domain="example.com", name="api", value="198.51.100.10"),),
    )
    explanation = compile_for(inputs).explanations[0]
    assert explanation.rule == "api"
    settings = {item.setting: item for item in explanation.settings}
    assert settings["cache_enabled"].origin is SettingOrigin.RULE
    assert settings["cache_enabled"].rule == "api"
    # The zone still decides everything the rule did not name, and the
    # explanation must not credit the rule with leaving it alone.
    assert settings["compression"].rule is None


def test_a_group_a_rule_did_not_claim_is_explained_against_the_zone():
    rule = Rule(
        domain="example.com",
        name="api",
        match="api.example.com",
        priority=10,
        overrides={"cache_enabled": False},
    )
    inputs = inputs_for(
        rules=(rule,),
        records=(DnsRecord(domain="example.com", name="www", value="198.51.100.10"),),
    )
    assert compile_for(inputs).explanations[0].rule is None


# -- Inputs ---------------------------------------------------------------


def test_inputs_round_trip_through_their_encoded_form():
    inputs = inputs_for(cache_enabled=False)
    assert ReleaseInputs.decode(inputs.encode()) == inputs


def test_inputs_of_an_unknown_schema_version_are_refused_by_version():
    with pytest.raises(ValueError, match="schema version"):
        ReleaseInputs.decode('{"schema_version": 99, "domains": []}')


def test_inputs_that_are_not_an_object_are_refused():
    with pytest.raises(ValueError, match="not an object"):
        ReleaseInputs.decode("[]")


def test_inputs_that_are_not_json_are_refused_as_json():
    with pytest.raises(ValueError, match="not valid JSON"):
        ReleaseInputs.decode("{oh no")
