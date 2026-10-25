"""The feature mechanism: declaration, the roster, binding, refusal.

Built-in features reach the API, the CLI, the scheduler and the desired-state
document through typed declarations and the roster's bindings. No contribution
is ever handed the composition root.
"""

from __future__ import annotations

from dataclasses import MISSING, fields
from importlib import import_module
from types import SimpleNamespace

import pytest
from fastapi import APIRouter
from support.control_plane import FakeRunner, build_application
from support.policy import site_from_settings
from typer import Typer

from blitzecdn.capabilities.deployments.wiring import deployments_operations
from blitzecdn.capabilities.diagnostics.wiring import diagnostics_operations
from blitzecdn.capabilities.dns.wiring import RENDERING as DNS_RENDERING
from blitzecdn.capabilities.http.http3.wiring import RENDERING as HTTP3_RENDERING
from blitzecdn.capabilities.http.wiring import RENDERING as HTTP_RENDERING
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.capabilities.tls.settings import CertificateConfig
from blitzecdn.capabilities.tls.wiring import certificates_operations
from blitzecdn.composition import (
    BUILTIN_FEATURES,
    ROSTER,
    Repository,
    build_api,
    build_worker,
    load_registry,
)
from blitzecdn.core.exceptions import FeatureError
from blitzecdn.core.features import (
    AnsibleContribution,
    CapabilityConfig,
    CliCommandGroup,
    Feature,
    FeatureMetadata,
    FeatureOperations,
    FeatureRegistry,
    FeatureRendering,
    FleetStateContribution,
    HealthCheck,
    NginxContribution,
    ScheduledJob,
    Severity,
    SiteStateContribution,
    ValidationIssue,
    load_features,
    merge_variables,
)
from blitzecdn.entrypoints.cli.main import register_commands

#: Features that only realize an edge capability: roles and fragments, no
#: service. A registry without `geoip` and `http3` lets tests see one absent.
_EDGE_PROVIDERS = frozenset(
    {
        "blitzecdn.capabilities.hardening.feature",
        "blitzecdn.capabilities.http.http3.feature",
        "blitzecdn.capabilities.resolver.feature",
        "blitzecdn.capabilities.security.geoip.feature",
    }
)
_CAPABILITY_FEATURES = tuple(
    path for path in BUILTIN_FEATURES if path not in _EDGE_PROVIDERS
)


@pytest.fixture
def baseline() -> FeatureRegistry:
    """The capability features this application ships, without edge providers."""
    return FeatureRegistry(load_features(_CAPABILITY_FEATURES))


def _capability_config(**values: object) -> SimpleNamespace:
    """A `ResolvedCapabilityEnvironment` double scoped the way the real one is."""
    resolved = {"BLITZE_" + name.upper(): value for name, value in values.items()}
    return SimpleNamespace(
        for_feature=lambda _name: CapabilityConfig("certificates", {}, resolved)
    )


def site(**overrides: object) -> CdnSite:
    return site_from_settings(
        {
            "name": "example-com",
            "server_names": ["cdn.example.com"],
            "origin_host": "198.51.100.10",
            "compression": "off",
            "cache_enabled": False,
            **overrides,
        }
    )


def _drift(calls: list[str], interval: int = 900) -> FeatureOperations:
    return deployments_operations(
        deployments=SimpleNamespace(
            check_drift=lambda _operator: calls.append("drift"),
            initialize=lambda: calls.append("initialize"),
        ),
        drift_check_interval_seconds=interval,
    )


# --- built-in features ------------------------------------------------------


def test_every_built_in_feature_is_declared(baseline):
    declared = {
        feature.name
        for module in _CAPABILITY_FEATURES
        for feature in import_module(module).FEATURES
    }
    assert {feature.name for feature in baseline.metadata} == declared


def test_a_built_in_that_cannot_be_imported_stops_the_process():
    """No degraded mode: a control plane missing `dns` renders an empty fleet."""
    with pytest.raises(FeatureError, match="could not be imported"):
        load_features(("blitzecdn.capabilities.nonexistent.feature",))


def test_a_built_in_module_has_to_declare_its_features():
    with pytest.raises(FeatureError, match="must export FEATURES"):
        load_features(("blitzecdn.capabilities.dns.wiring",))


def test_two_built_in_modules_cannot_declare_one_feature():
    with pytest.raises(FeatureError, match="both declare a feature named 'dns'"):
        load_features(
            (
                "blitzecdn.capabilities.dns.feature",
                "blitzecdn.capabilities.dns.feature",
            )
        )


def test_the_built_in_registration_order_does_not_change_desired_state():
    """Reordering the roster is a presentation change and nothing more."""
    renderings = (DNS_RENDERING, HTTP_RENDERING, HTTP3_RENDERING)
    forward = FeatureRegistry(load_features(BUILTIN_FEATURES))
    backward = FeatureRegistry(load_features(tuple(reversed(BUILTIN_FEATURES))))
    subject = site()

    assert forward.bind_rendering(renderings).site_variables(
        subject
    ) == backward.bind_rendering(tuple(reversed(renderings))).site_variables(subject)


# --- binding ----------------------------------------------------------------


def test_bound_behaviour_has_to_belong_to_a_declared_feature(baseline):
    with pytest.raises(FeatureError, match="'nope' names no built-in feature"):
        baseline.bind_rendering((FeatureRendering(feature="nope"),))


def test_a_feature_binds_its_behaviour_once(baseline):
    with pytest.raises(FeatureError, match="'dns' binds its rendering twice"):
        baseline.bind_rendering((DNS_RENDERING, DNS_RENDERING))


def test_rendering_merges_every_bound_feature(baseline):
    variables = baseline.bind_rendering(
        (DNS_RENDERING, HTTP_RENDERING)
    ).fleet_variables((site(),))

    assert variables == {
        "blitzecdn_edge_http3_enabled": False,
        "blitzecdn_nginx_http3_listener_owner": "",
    }


# --- the roster -------------------------------------------------------------


def test_the_roster_names_each_feature_module_once():
    assert len(set(BUILTIN_FEATURES)) == len(BUILTIN_FEATURES) == len(ROSTER)
    assert {feature.name for feature in load_registry().metadata} >= {
        "dns",
        "tls",
        "certificates",
        "geoip",
        "http3",
    }


def test_every_roster_builder_binds_behaviour_its_own_module_declares(settings):
    """A row's builders belong to the features its module declares.

    Otherwise a feature would be registered in two places.
    """
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )
    declared = {
        entry.module: {feature.name for feature in import_module(entry.module).FEATURES}
        for entry in ROSTER
    }
    bound = {
        *((item.feature, "rendering") for item in control.rendering._renderings),
        *((item.feature, "operations") for item in control.operations.operations),
    }
    rows = {
        (name, kind)
        for entry in ROSTER
        for kind, builder in (
            ("rendering", entry.rendering),
            ("operations", entry.operations),
        )
        if builder is not None
        for name in declared[entry.module]
    }
    assert bound
    assert {name for name, _ in bound} <= {name for name, _ in rows}
    control.close()


# --- contributions ----------------------------------------------------------


def test_a_feature_that_ships_no_roles_contributes_no_directory(baseline):
    """Most features have no Ansible of their own, and say nothing rather than
    an empty path that core would then have to interpret."""
    contributing = {item.feature for item in baseline.ansible_contributions()}

    assert contributing == {
        feature.name for feature in baseline.features if feature.ansible
    }
    assert "dns" not in contributing


# --- names and capability tokens -------------------------------------------


def test_a_registry_refuses_two_features_with_one_name(baseline):
    dns = next(feature for feature in baseline.features if feature.name == "dns")

    with pytest.raises(FeatureError, match="two features claim the name 'dns'"):
        FeatureRegistry((*baseline.features, dns))


def test_a_capability_token_says_what_a_configuration_may_depend_on(baseline):
    """`provides` is separate from `name`, and defaults to it.

    A feature identifies itself with `name`; a configuration depends on a
    capability token, which another feature may supply.
    """
    assert "dns" in baseline.capabilities
    assert baseline.missing(["dns", "waf"]) == ("waf",)

    metadata = FeatureMetadata(
        name="acme-waf",
        version="1.0",
        provides=frozenset({"waf", "ratelimit"}),
    )
    assert metadata.capabilities == {"acme-waf", "waf", "ratelimit"}


def test_requiring_a_capability_nothing_provides_names_it(baseline):
    """The message an operator reads when configuration requires an unknown token.

    It names the missing token and what is available, so a typo is told apart
    from an absent capability.
    """
    baseline.require(["dns"], subject="a test")

    with pytest.raises(FeatureError) as failure:
        baseline.require(["dns", "waf"], subject="this installation")

    message = str(failure.value)
    assert "waf" in message
    assert "this installation" in message
    assert "dns" in message


# --- ordering ---------------------------------------------------------------


def test_contributions_come_back_in_registration_order(baseline):
    """Features in roster order."""
    names = [group.name for group in baseline.cli_commands()]

    assert names.index("domain") < names.index("edge") < names.index("backup")


# --- merging desired state --------------------------------------------------


def test_a_declared_override_wins_wherever_it_registered():
    base = SiteStateContribution(
        feature="dns", variables={"certificate_path": "/model"}
    )
    override = SiteStateContribution(
        feature="certificates",
        variables={"certificate_path": "/real"},
        overrides=frozenset({"certificate_path"}),
    )

    assert merge_variables([base, override], subject="s") == {
        "certificate_path": "/real"
    }
    assert merge_variables([override, base], subject="s") == {
        "certificate_path": "/real"
    }


def test_two_features_writing_one_variable_is_a_conflict_not_a_race():
    contributions = [
        SiteStateContribution(
            feature="cache", variables={"cache_valid_success": "10m"}
        ),
        SiteStateContribution(feature="waf", variables={"cache_valid_success": "1h"}),
    ]

    with pytest.raises(FeatureError, match="cache, waf both set 'cache_valid_success'"):
        merge_variables(contributions, subject="desired state")


def test_two_features_cannot_both_claim_to_override_one_variable():
    contributions = [
        SiteStateContribution(
            feature="a", variables={"x": 1}, overrides=frozenset({"x"})
        ),
        SiteStateContribution(
            feature="b", variables={"x": 2}, overrides=frozenset({"x"})
        ),
    ]

    with pytest.raises(FeatureError, match="each claim to override 'x'"):
        merge_variables(contributions, subject="desired state")


def test_an_override_of_a_variable_nobody_else_writes_is_allowed():
    """Whether the site model happens to carry a path is not the certificate
    feature's business, so overriding an absent key must not be an error."""
    contribution = SiteStateContribution(
        feature="certificates",
        variables={"certificate_path": "/real"},
        overrides=frozenset({"certificate_path"}),
    )

    assert merge_variables([contribution], subject="s") == {"certificate_path": "/real"}


def test_fleet_variables_merge_under_the_same_rule():
    contributions = [
        FleetStateContribution(feature="dns", variables={"a": True}),
        FleetStateContribution(feature="waf", variables={"b": False}),
    ]

    assert merge_variables(contributions, subject="fleet") == {"a": True, "b": False}


# --- deployment checks ------------------------------------------------------


def test_deployment_checks_are_collected_from_every_feature(baseline):
    def objections(subject: CdnSite) -> list[ValidationIssue]:
        return [
            ValidationIssue(
                feature="security", site=subject.name, message="no rule set loaded"
            ),
            ValidationIssue(
                feature="security",
                site=subject.name,
                message="rule set is a week old",
                severity=Severity.WARNING,
            ),
        ]

    result = baseline.bind_rendering(
        (FeatureRendering(feature="security", deployment_checks=objections),)
    ).validate_site(site())

    assert not result.ok
    assert [issue.message for issue in result.blocking] == ["no rule set loaded"]
    assert len(result.issues) == 2


def test_a_site_no_feature_objects_to_is_deployable(baseline):
    assert baseline.bind_rendering(()).validate_site(site()).ok


def test_a_requested_capability_nothing_supplies_blocks_and_names_the_setting():
    """The message an operator reads before any playbook runs.

    The tokens come from the site's `capability_requirements` and the answer from
    feature metadata, so nothing on this path knows what `geoip` is. The mapping
    adds which setting asked.
    """
    dns_only = FeatureRegistry(load_features(("blitzecdn.capabilities.dns.feature",)))
    requested = site(
        visitor_headers={"ip_country": True},
        firewall={"denied_countries": ["RU"]},
    )

    result = dns_only.bind_rendering(()).validate_site(requested)

    assert not result.ok
    messages = {issue.message for issue in result.blocking}
    geoip = next(message for message in messages if "'geoip'" in message)
    assert "firewall.denied_countries" in geoip
    assert "visitor_headers.ip_country" in geoip
    assert "is not installed" in geoip
    # The other token the same site asks for is reported separately rather than
    # folded in, so detaching either capability is legible on its own.
    assert any("'security'" in message for message in messages)


def test_a_capability_requested_by_one_setting_reads_in_the_singular(baseline):
    """One asker, one verb. The plural form is for a site that asked twice."""
    (issue,) = (
        baseline.bind_rendering(())
        .validate_site(site(visitor_headers={"ip_country": True}))
        .blocking
    )

    assert "site's visitor_headers.ip_country requests it" in issue.message


# --- lifecycle --------------------------------------------------------------


def test_startup_and_shutdown_reach_every_feature_in_order(baseline):
    seen: list[str] = []
    operations = baseline.bind_operations(
        (
            FeatureOperations(
                feature="jobs",
                startup=lambda: seen.append("jobs start"),
                shutdown=lambda: seen.append("jobs stop"),
            ),
            FeatureOperations(
                feature="workflows",
                startup=lambda: seen.append("workflows start"),
                shutdown=lambda: seen.append("workflows stop"),
            ),
        )
    )

    operations.startup()
    operations.shutdown()

    assert seen == [
        "jobs start",
        "workflows start",
        "workflows stop",
        "jobs stop",
    ]


def test_deployment_recovery_runs_when_the_api_starts_and_not_the_worker(
    settings, monkeypatch
):
    """Node startup is the API's; the worker only publishes schedules."""
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        control.deployments, "initialize", lambda: calls.append("initialize")
    )

    build_worker(control).start()
    assert calls == []
    build_api(control).start()
    assert calls == ["initialize"]
    control.close()


# --- scheduled jobs ---------------------------------------------------------


def test_two_features_cannot_contribute_one_job_name(baseline):
    clasher = FeatureOperations(
        feature="jobs",
        scheduled_jobs=(
            ScheduledJob(
                feature="jobs",
                name="check-drift",
                interval_seconds=60,
                run=lambda _o: None,
            ),
        ),
    )

    with pytest.raises(FeatureError, match="'deployments' and 'jobs'"):
        baseline.bind_operations((_drift([]), clasher))


def test_every_built_in_job_calls_the_service_that_owns_its_work(baseline):
    """A job is one object — its interval and its work — so both are checkable."""
    calls: list[str] = []
    jobs = baseline.bind_operations((_drift(calls),)).scheduled_jobs

    assert set(jobs) == {"check-drift"}
    jobs["check-drift"].run("scheduler")

    assert calls == ["drift"]
    assert jobs["check-drift"].interval_seconds == 900


def test_a_disabled_interval_is_how_a_job_is_turned_off(baseline):
    config = CertificateConfig.from_capability_config(
        _capability_config(
            certbot="certbot",
            acme_default_email="",
            acme_ca_domain="letsencrypt.org",
            certificate_reconcile_interval_seconds=0,
            certificate_renewal_interval_seconds=0,
            certificate_renewal_budget_seconds=300,
            ssl_automatic_scan_interval_seconds=0,
        ).for_feature("certificates")
    )
    operations = baseline.bind_operations(
        (
            _drift([], interval=0),
            certificates_operations(
                certificates=SimpleNamespace(),
                automatic_ssl=SimpleNamespace(),
                config=config,
                deployment_timeout_seconds=900,
            ),
        )
    )

    assert len(operations.scheduled_jobs) == 4
    assert all(job.interval_seconds == 0 for job in operations.scheduled_jobs.values())


# --- feature metadata --------------------------------------------------------


def test_a_feature_name_has_to_be_usable_as_an_identifier():
    with pytest.raises(ValueError, match="must be alphanumeric"):
        FeatureMetadata(name="not a name!", version="1.0")


# --- the contributions a feature makes are values, not calls ------------------


def test_a_contributed_group_may_carry_root_level_verbs(baseline):
    """`deploy` is a verb an operator types, not a noun to nest it under."""
    rootless = [group for group in baseline.cli_commands() if group.name is None]

    assert rootless
    assert all(isinstance(group.app, Typer) for group in rootless)


def test_routers_are_real_routers(baseline):
    assert all(isinstance(router, APIRouter) for router in baseline.api_routers())


def test_a_health_check_fails_by_raising():
    def unhealthy() -> None:
        raise ConnectionError("no")

    check = HealthCheck(feature="waf", name="x", check=unhealthy)

    with pytest.raises(ConnectionError):
        check.check()


def test_a_command_group_names_the_subcommand_it_appears_under():
    group = CliCommandGroup(feature="waf", name="waf", app=Typer())

    assert group.name == "waf"


def _registry(*groups: CliCommandGroup) -> FeatureRegistry:
    return FeatureRegistry(
        [
            Feature(
                metadata=FeatureMetadata(name=f"f{index}", version="1.0"),
                cli_commands=(group,),
            )
            for index, group in enumerate(groups)
        ]
    )


def _verbs(*names: str) -> Typer:
    app = Typer()
    for name in names:
        app.command(name)(lambda: None)
    return app


def test_an_extending_group_joins_the_founder_in_its_declared_order():
    founder = _verbs("add", "show")
    root = Typer()
    registry = _registry(
        CliCommandGroup(feature="waf", name="zone", app=_verbs("cache"), extends=True),
        CliCommandGroup(
            feature="dns", name="zone", app=founder, order=("add", "cache", "show")
        ),
    )

    register_commands(registry, root)
    register_commands(registry, root)

    assert [command.name for command in founder.registered_commands] == [
        "add",
        "cache",
        "show",
    ]
    assert len(root.registered_groups) == 2


def test_a_command_the_founder_did_not_place_refuses_to_start():
    registry = _registry(
        CliCommandGroup(feature="dns", name="zone", app=_verbs("add"), order=("add",)),
        CliCommandGroup(feature="waf", name="zone", app=_verbs("waf"), extends=True),
    )

    with pytest.raises(FeatureError, match="waf"):
        register_commands(registry, Typer())


def test_extending_a_group_nobody_founds_is_refused():
    registry = _registry(
        CliCommandGroup(feature="waf", name="zone", app=_verbs("waf"), extends=True)
    )

    with pytest.raises(FeatureError, match="no installed feature founds"):
        register_commands(registry, Typer())


def test_two_features_cannot_found_one_group():
    registry = _registry(
        CliCommandGroup(feature="dns", name="zone", app=Typer()),
        CliCommandGroup(feature="waf", name="zone", app=Typer()),
    )

    with pytest.raises(FeatureError, match="both found"):
        register_commands(registry, Typer())


def test_only_the_founding_group_sets_the_order():
    with pytest.raises(ValueError, match="only the group that founded it"):
        CliCommandGroup(
            feature="waf", name="zone", app=Typer(), extends=True, order=("a",)
        )


def test_a_contribution_into_a_shared_namespace_says_who_contributed_it():
    """Registration under a fleet-wide name carries its owning feature.

    Without it, duplicate errors cannot say which features collided and the
    frozen CLI surface has to guess ownership. Any contribution type registered
    under a shared name must declare `feature`.
    """
    shared = (
        AnsibleContribution,
        CliCommandGroup,
        FeatureOperations,
        FeatureRendering,
        FleetStateContribution,
        HealthCheck,
        NginxContribution,
        ScheduledJob,
        SiteStateContribution,
        ValidationIssue,
    )
    missing = [
        contribution.__name__
        for contribution in shared
        if not any(
            field.name == "feature"
            and field.default is MISSING
            and field.default_factory is MISSING
            for field in fields(contribution)
        )
    ]
    assert missing == []


def test_two_features_cannot_answer_to_one_health_check_name(baseline):
    """`/health` reports a check by name, so the name has to be one feature's."""
    clasher = FeatureOperations(
        feature="jobs",
        health_checks=(
            HealthCheck(feature="jobs", name="database", check=lambda: None),
        ),
    )

    with pytest.raises(FeatureError, match="'diagnostics' and 'jobs'"):
        baseline.bind_operations(
            (
                diagnostics_operations(
                    read_database=lambda: None, queue_ready=lambda: True
                ),
                clasher,
            )
        )
