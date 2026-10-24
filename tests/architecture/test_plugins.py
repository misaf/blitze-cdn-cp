"""The feature and extension mechanism: declaration, discovery, binding, refusal.

Three things are held here. Built-in features reach the API, the CLI, the
scheduler and the desired-state document through typed declarations and
bindings, not through a list somebody maintains. A package this repository has
never heard of can do the same through hooks — so the external tests load
`tests/external_plugins/*` through the real entry-point machinery, because the
path an operator's `pip install` takes is the path worth testing. And neither
kind of contribution is ever handed the composition root.
"""

from __future__ import annotations

import ast
from dataclasses import MISSING, fields
from importlib import import_module
from importlib.metadata import EntryPoint
from types import SimpleNamespace

import pytest
from fastapi import APIRouter
from paths import REPO_ROOT, SOURCE
from policy_support import site_from_settings
from typer import Typer

from blitzecdn.capabilities.deployments.runtime import deployments_operations
from blitzecdn.capabilities.diagnostics.runtime import diagnostics_operations
from blitzecdn.capabilities.dns.runtime import RENDERING as DNS_RENDERING
from blitzecdn.capabilities.http.runtime import RENDERING as HTTP_RENDERING
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.capabilities.tls.config import CertificateConfig
from blitzecdn.capabilities.tls.runtime import certificates_operations
from blitzecdn.composition import BUILTIN_FEATURES, load_control_plane_plugins
from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins import (
    ENTRY_POINT_GROUP,
    HOOK_API_VERSION,
    SUPPORTED_HOOK_API_VERSIONS,
    AnsibleContribution,
    CapabilityConfig,
    CliCommandGroup,
    FeatureOperations,
    FeatureRendering,
    FleetStateContribution,
    HealthCheck,
    NginxContribution,
    PluginMetadata,
    PluginRegistry,
    ProcessKind,
    ScheduledJob,
    Severity,
    SiteStateContribution,
    ValidationIssue,
    build_plugin_manager,
    hookimpl,
    load_plugins,
    merge_variables,
    register,
    register_external,
)
from blitzecdn.core.plugins.resolution import resolve_role_search_path
from blitzecdn.providers.http3.runtime import RENDERING as HTTP3_RENDERING

_FIXTURES = "external_plugins"

#: The capability features, without the focused providers. A registry missing
#: `geoip` and `http3` is what lets these tests watch a capability be absent.
_CAPABILITY_FEATURES = tuple(
    path for path in BUILTIN_FEATURES if path.startswith("blitzecdn.capabilities.")
)


def entry_point(name: str, module: str) -> EntryPoint:
    """One entry point, exactly as an installed distribution would declare it."""
    return EntryPoint(name=name, value=f"{_FIXTURES}.{module}", group=ENTRY_POINT_GROUP)


def attach(registry: PluginRegistry, *points: EntryPoint):
    """Install external plugins into ``registry``, as discovery at startup would."""
    return register_external(
        registry._manager,
        points=points,
        reserved={feature.name for feature in registry.features},
    )


@pytest.fixture
def baseline() -> PluginRegistry:
    """The capabilities this distribution ships, and nothing an environment added.

    `entry_point_group=None` on purpose: a test asserting on the built-in set
    must not change its answer because a developer happens to have an
    unrelated BlitzeCDN plugin installed in the same virtualenv.
    """
    return load_plugins(_CAPABILITY_FEATURES, entry_point_group=None)


def _capability_config(**values: object) -> SimpleNamespace:
    """A `ResolvedCapabilityEnvironment` double scoped the way the real one is."""
    resolved = {"BLITZE_" + name.upper(): value for name, value in values.items()}
    return SimpleNamespace(
        for_plugin=lambda _name: CapabilityConfig("certificates", {}, resolved)
    )


def context(process: ProcessKind = ProcessKind.WORKER) -> SimpleNamespace:
    """An `ExtensionContext` double: ports and configuration, no services."""
    return SimpleNamespace(
        process=process,
        settings=SimpleNamespace(),
        capability_config=_capability_config(),
        sites=SimpleNamespace(list_sites=list),
        fleet=SimpleNamespace(),
        events=SimpleNamespace(),
        queue_ready=lambda: True,
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


# --- the hook specifications ------------------------------------------------


def test_the_hook_contract_is_small_and_every_hook_is_a_registration_point():
    """A hook is somewhere core has to be *told* something exists.

    The list is asserted whole rather than counted, because the failure this
    guards is not "there are too many" but "someone added
    `blitzecdn_issue_certificate`" — a business call wearing a hook's clothes.
    """
    manager = build_plugin_manager()
    assert set(manager.hook.__dict__) - {"_needs_discovery"} == {
        "blitzecdn_plugin_metadata",
        "blitzecdn_api_routers",
        "blitzecdn_cli_commands",
        "blitzecdn_ansible_contributions",
        "blitzecdn_capability_configuration",
        "blitzecdn_nginx_contributions",
        "blitzecdn_health_checks",
        "blitzecdn_scheduled_jobs",
        "blitzecdn_site_desired_state",
        "blitzecdn_fleet_desired_state",
        "blitzecdn_deployment_checks",
        "blitzecdn_startup",
        "blitzecdn_shutdown",
    }


def test_no_hook_is_handed_the_composition_root():
    """Every non-static hook takes an `ExtensionContext`, and nothing else does."""
    from blitzecdn.core.plugins import hooks

    for name, function in vars(hooks).items():
        if not name.startswith("blitzecdn_"):
            continue
        parameters = set(function.__code__.co_varnames[: function.__code__.co_argcount])
        assert "platform" not in parameters, name
        assert parameters <= {"site", "sites", "context"}, name


# --- built-in features ------------------------------------------------------


def test_every_built_in_feature_is_declared_and_required(baseline):
    declared = {
        feature.name
        for module in _CAPABILITY_FEATURES
        for feature in import_module(module).FEATURES
    }
    assert {plugin.name for plugin in baseline.plugins} == declared
    assert all(plugin.required for plugin in baseline.plugins)
    assert baseline.rejected == ()


def test_a_strategy_or_mode_never_declares_a_feature_module():
    """The capability declares a feature; the option inside it does not.

    `http3` was a capability once. So were `certificates` and `automatic_ssl`,
    which is how one capability came to have two registrations and two owners
    of `SslMode`. Each name here would be a plausible package, and each belongs
    inside one that already exists.
    """
    for capability in ("http", "tls", "dns", "compression", "security"):
        assert f"blitzecdn.capabilities.{capability}.feature" in BUILTIN_FEATURES

    for option in (
        "gzip",
        "brotli",
        "http3",
        "under_attack",
        "certificates",
        "automatic_ssl",
        "geoip",
    ):
        assert f"blitzecdn.capabilities.{option}.feature" not in BUILTIN_FEATURES


def test_a_built_in_that_cannot_be_imported_stops_the_process():
    """No degraded mode: a control plane missing `dns` renders an empty fleet."""
    with pytest.raises(PluginError, match="could not be imported"):
        load_plugins(builtins=("blitzecdn.capabilities.nonexistent.feature",))


def test_a_built_in_module_has_to_declare_its_features():
    with pytest.raises(PluginError, match="must export FEATURES"):
        load_plugins(builtins=(f"{_FIXTURES}.external",), entry_point_group=None)


def test_a_built_in_that_declares_itself_optional_is_refused():
    with pytest.raises(PluginError, match="declares itself optional"):
        load_plugins(
            builtins=(f"{_FIXTURES}.optional_feature",), entry_point_group=None
        )


def test_two_built_in_modules_cannot_declare_one_feature():
    with pytest.raises(PluginError, match="both declare a feature named 'dns'"):
        load_plugins(
            builtins=(
                "blitzecdn.capabilities.dns.feature",
                "blitzecdn.capabilities.dns.feature",
            ),
            entry_point_group=None,
        )


def test_the_built_in_registration_order_does_not_change_desired_state():
    """Reordering `BUILTIN_FEATURES` is a presentation change and nothing more."""
    renderings = (DNS_RENDERING, HTTP_RENDERING, HTTP3_RENDERING)
    forward = load_plugins(BUILTIN_FEATURES, entry_point_group=None)
    backward = load_plugins(tuple(reversed(BUILTIN_FEATURES)), entry_point_group=None)
    subject = site()

    assert forward.bind_rendering(renderings, context()).site_variables(
        subject
    ) == backward.bind_rendering(tuple(reversed(renderings)), context()).site_variables(
        subject
    )


# --- binding ----------------------------------------------------------------


def test_bound_behaviour_has_to_belong_to_a_declared_feature(baseline):
    with pytest.raises(PluginError, match="'nope' names no built-in feature"):
        baseline.bind_rendering((FeatureRendering(plugin="nope"),), context())


def test_a_feature_binds_its_behaviour_once(baseline):
    with pytest.raises(PluginError, match="'dns' binds its rendering twice"):
        baseline.bind_rendering((DNS_RENDERING, DNS_RENDERING), context())


def test_rendering_merges_every_bound_feature(baseline):
    variables = baseline.bind_rendering(
        (DNS_RENDERING, HTTP_RENDERING), context()
    ).fleet_variables((site(),))

    assert variables == {
        "blitzecdn_edge_http3_enabled": False,
        "blitzecdn_nginx_http3_listener_owner": "",
    }


# --- external discovery through entry points --------------------------------


def test_an_external_plugin_is_discovered_through_its_entry_point(baseline):
    found = attach(baseline, entry_point("waf", "external"))

    assert [plugin.name for plugin in found.plugins] == ["waf"]
    assert found.rejected == ()
    assert "/v1/waf/status" in {
        route.path for router in baseline.api_routers() for route in router.routes
    }
    assert "waf" in {group.name for group in baseline.cli_commands()}


def test_an_external_plugin_contributes_jobs_health_checks_and_state(baseline):
    attach(baseline, entry_point("waf", "external"))
    operations = baseline.bind_operations((), context())
    rendering = baseline.bind_rendering((), context())

    assert "waf-rule-refresh" in operations.scheduled_jobs
    assert "waf-rules" in {check.name for check in operations.health_checks}
    assert rendering.site_variables(site())["waf_mode"] == "enforce"
    assert rendering.fleet_variables((site(),))["blitzecdn_edge_waf_enabled"]


def test_an_installed_plugin_contributes_its_ansible_roles(baseline):
    """The deployment half of a capability, discovered the same way as the rest.

    Nothing in core names this plugin, and nothing in core copies its files
    anywhere. It reports the directory its wheel put them in and the control
    plane composes that into Ansible's search path.
    """
    attach(baseline, entry_point("waf", "external"))
    contributions = baseline.ansible_contributions()

    assert contributions[-1].plugin == "waf"
    assert (contributions[-1].roles_path / "blitzecdn_waf/tasks/main.yml").is_file()


def test_a_feature_that_ships_no_roles_contributes_no_directory(baseline):
    """Most features have no Ansible of their own, and say nothing rather than
    an empty path that core would then have to interpret."""
    contributing = {item.plugin for item in baseline.ansible_contributions()}

    assert contributing == {
        feature.name for feature in baseline.features if feature.ansible
    }
    assert "dns" not in contributing


def test_the_roles_of_an_absent_plugin_are_absent_from_the_search_path(
    baseline, tmp_path
):
    """Detach, in the terms Ansible sees.

    The registry with no `waf` registered yields no contribution for it, so its
    role name simply stops resolving, which is what "the capability is not
    installed" has to mean on the edge as well as in the API.
    """
    core = tmp_path / "roles"
    (core / "blitzecdn_nginx").mkdir(parents=True)
    detached = resolve_role_search_path(core, baseline.ansible_contributions())

    attach(baseline, entry_point("waf", "external"))
    attached = resolve_role_search_path(core, baseline.ansible_contributions())

    assert not any((path / "blitzecdn_waf").is_dir() for path in detached)
    assert len(attached) == len(detached) + 1
    assert (attached[-1] / "blitzecdn_waf").is_dir()


def test_core_needs_no_change_for_an_external_capability(baseline):
    """The acceptance criterion, stated as a diff that is not required.

    Everything the WAF plugin adds — a route, a command, a job, a check, its
    share of every site's desired state — arrives without `BUILTIN_FEATURES`,
    `api/app.py`, `cli/main.py` or the release compiler knowing it exists.
    """
    before = len(baseline.api_routers())
    attach(baseline, entry_point("waf", "external"))

    assert len(baseline.api_routers()) == before + 1
    assert f"{_FIXTURES}.external" not in BUILTIN_FEATURES


# --- the failure policy -----------------------------------------------------


def test_an_external_plugin_that_raises_on_import_is_skipped_with_its_reason(
    baseline, caplog
):
    """Skipped, never silent: the reason is logged *and* kept for an operator."""
    with caplog.at_level("WARNING"):
        found = attach(baseline, entry_point("waf", "broken"))

    assert found.plugins == ()
    assert [rejection.source for rejection in found.rejected] == [
        f"waf ({_FIXTURES}.broken)"
    ]
    assert "this plugin is broken" in str(found.rejected[0])
    assert "this plugin is broken" in caplog.text


def test_a_plugin_that_will_not_say_who_it_is_contributes_nothing(baseline):
    """Anonymous contributions cannot be attributed, so they are not accepted."""
    found = attach(baseline, entry_point("anonymous", "anonymous"))

    assert "no blitzecdn_plugin_metadata hook" in str(found.rejected[0])
    assert "anonymous" not in {
        check.name for check in baseline.bind_operations((), context()).health_checks
    }


def test_a_plugin_states_the_hook_contract_it_was_written_against():
    """As a literal, because no value core can supply means what this field says.

    `api_version` defaulted to `HOOK_API_VERSION` — core's own constant, read
    at the plugin's import time — so a plugin that said nothing agreed with
    whatever control plane it was loaded into, however old the hooks it
    actually implements. Every plugin in this workspace omitted it, so the
    check had never once been in a position to fail; the only author it could
    ever have refused was the one who pinned a number honestly.

    Writing `api_version=HOOK_API_VERSION` reintroduces exactly that, one level
    up, and looks more correct than the literal while meaning less. So the
    field takes an integer that was true when somebody typed it, and this
    refuses the import for the features this workspace ships.

    Third-party wheels cannot be held to it here. What holds them is that the
    natural thing to write — a number — is the right one, and that omitting it
    is now a `TypeError` at construction rather than a silent agreement.
    """
    offenders = []
    for path in sorted(SOURCE.rglob("feature.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "PluginMetadata"
            ):
                continue
            stated = {
                keyword.arg: keyword.value
                for keyword in node.keywords
                if keyword.arg == "api_version"
            }
            value = stated.get("api_version")
            if value is None:
                offenders.append(f"{path.name} does not state api_version")
            elif not isinstance(value, ast.Constant) or not isinstance(
                value.value, int
            ):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)} passes "
                    f"{ast.unparse(value)}; write the number it was built against"
                )
    assert offenders == []


def test_the_supported_contract_set_is_how_a_bump_gets_a_deprecation_window():
    """The gate is membership, not equality.

    A set makes a transition expressible: widen it to keep the previous
    contract loading while authors move, and drop that version later as a
    separate decision visible in one line. The version core *speaks* has to be
    one it *accepts*, or it would refuse a plugin written against itself.
    """
    assert HOOK_API_VERSION in SUPPORTED_HOOK_API_VERSIONS


def test_a_plugin_from_a_later_hook_contract_is_refused_by_version(baseline):
    found = attach(baseline, entry_point("future", "from_the_future"))

    assert found.plugins == ()
    assert "targets hook API v99" in str(found.rejected[0])


def test_an_external_plugin_cannot_take_a_built_in_name(baseline):
    """An installed package must not take a built-in's identity and answer for it."""
    found = attach(baseline, entry_point("impostor", "impostor"))

    assert found.plugins == ()
    assert "which a built-in feature already uses" in str(found.rejected[0])
    assert "dns" in baseline


def test_two_installed_packages_cannot_claim_one_name(baseline):
    """The same rule between two external distributions, neither built in.

    The second is refused with its source, and the first keeps the name and
    keeps working.
    """
    first = attach(baseline, entry_point("waf", "external"))
    second = attach(baseline, entry_point("waf-again", "external"))

    assert [plugin.name for plugin in first.plugins] == ["waf"]
    assert second.plugins == ()
    assert "already registered" in str(second.rejected[0])
    assert "waf-again" in str(second.rejected[0])


def test_a_registry_refuses_two_plugins_with_one_name(baseline):
    impostor = PluginMetadata(name="dns", version="0.1", api_version=2)

    with pytest.raises(PluginError, match="two plugins claim the name 'dns'"):
        PluginRegistry(baseline.features, external=(impostor,))


def test_a_capability_token_says_what_a_configuration_may_depend_on(baseline):
    """`provides` is separate from `name`, and defaults to it.

    A plugin identifies itself with `name`; a *configuration* depends on a
    capability. The two are allowed to differ, which is what lets a replacement
    implementation answer for a token another package used to supply.
    """
    assert "dns" in baseline.capabilities
    assert baseline.missing(["dns", "waf"]) == ("waf",)

    metadata = PluginMetadata(
        name="acme-waf",
        version="1.0",
        api_version=2,
        provides=frozenset({"waf", "ratelimit"}),
    )
    assert metadata.capabilities == {"acme-waf", "waf", "ratelimit"}


def test_requiring_a_capability_nothing_provides_names_it(baseline):
    """The message an operator reads when a package they need is not installed.

    It has to name the missing token *and* what is installed, or an operator
    cannot tell a typo from an uninstalled package.
    """
    baseline.require(["dns"], subject="a test")

    with pytest.raises(PluginError) as failure:
        baseline.require(["dns", "waf"], subject="this installation")

    message = str(failure.value)
    assert "waf" in message
    assert "this installation" in message
    assert "dns" in message


def test_one_broken_plugin_does_not_stop_the_others(baseline):
    found = attach(
        baseline, entry_point("broken", "broken"), entry_point("waf", "external")
    )

    assert [plugin.name for plugin in found.plugins] == ["waf"]
    assert len(found.rejected) == 1


def test_a_plugin_returning_the_wrong_shape_is_named_rather_than_crashing(baseline):
    class Careless:
        @hookimpl
        def blitzecdn_plugin_metadata(self) -> PluginMetadata:
            return PluginMetadata(name="careless", version="1.0", api_version=2)

        @hookimpl
        def blitzecdn_api_routers(self) -> object:
            return ["not a router"]

    baseline._manager.register(Careless(), name="careless")

    with pytest.raises(PluginError, match="blitzecdn_api_routers returned a str"):
        baseline.api_routers()


# --- ordering ---------------------------------------------------------------


def test_contributions_come_back_in_registration_order(baseline):
    """Built-ins in roster order, then external plugins in their own order."""
    attach(baseline, entry_point("waf", "external"))
    names = [group.name for group in baseline.cli_commands()]

    assert names.index("domain") < names.index("edge") < names.index("waf")


# --- merging desired state --------------------------------------------------


def test_a_declared_override_wins_wherever_it_registered():
    base = SiteStateContribution(plugin="dns", variables={"certificate_path": "/model"})
    override = SiteStateContribution(
        plugin="certificates",
        variables={"certificate_path": "/real"},
        overrides=frozenset({"certificate_path"}),
    )

    assert merge_variables([base, override], subject="s") == {
        "certificate_path": "/real"
    }
    assert merge_variables([override, base], subject="s") == {
        "certificate_path": "/real"
    }


def test_two_plugins_writing_one_variable_is_a_conflict_not_a_race():
    contributions = [
        SiteStateContribution(plugin="cache", variables={"cache_valid_success": "10m"}),
        SiteStateContribution(plugin="waf", variables={"cache_valid_success": "1h"}),
    ]

    with pytest.raises(PluginError, match="cache, waf both set 'cache_valid_success'"):
        merge_variables(contributions, subject="desired state")


def test_two_plugins_cannot_both_claim_to_override_one_variable():
    contributions = [
        SiteStateContribution(
            plugin="a", variables={"x": 1}, overrides=frozenset({"x"})
        ),
        SiteStateContribution(
            plugin="b", variables={"x": 2}, overrides=frozenset({"x"})
        ),
    ]

    with pytest.raises(PluginError, match="each claim to override 'x'"):
        merge_variables(contributions, subject="desired state")


def test_an_override_of_a_variable_nobody_else_writes_is_allowed():
    """Whether the site model happens to carry a path is not the certificate
    plugin's business, so overriding an absent key must not be an error."""
    contribution = SiteStateContribution(
        plugin="certificates",
        variables={"certificate_path": "/real"},
        overrides=frozenset({"certificate_path"}),
    )

    assert merge_variables([contribution], subject="s") == {"certificate_path": "/real"}


def test_fleet_variables_merge_under_the_same_rule():
    contributions = [
        FleetStateContribution(plugin="dns", variables={"a": True}),
        FleetStateContribution(plugin="waf", variables={"b": False}),
    ]

    assert merge_variables(contributions, subject="fleet") == {"a": True, "b": False}


# --- deployment checks ------------------------------------------------------


def test_deployment_checks_are_collected_from_every_plugin(baseline):
    class Objector:
        @hookimpl
        def blitzecdn_plugin_metadata(self) -> PluginMetadata:
            return PluginMetadata(name="objector", version="1.0", api_version=2)

        @hookimpl
        def blitzecdn_deployment_checks(self, site: CdnSite) -> list[ValidationIssue]:
            return [
                ValidationIssue(
                    plugin="objector", site=site.name, message="no rule set loaded"
                ),
                ValidationIssue(
                    plugin="objector",
                    site=site.name,
                    message="rule set is a week old",
                    severity=Severity.WARNING,
                ),
            ]

    baseline._manager.register(Objector(), name="objector")
    result = baseline.bind_rendering((), context()).validate_site(site())

    assert not result.ok
    assert [issue.message for issue in result.blocking] == ["no rule set loaded"]
    assert len(result.issues) == 2


def test_a_site_no_plugin_objects_to_is_deployable(baseline):
    assert baseline.bind_rendering((), context()).validate_site(site()).ok


def test_a_requested_capability_nothing_supplies_blocks_and_names_the_setting():
    """The message an operator reads before any playbook runs.

    Generic in both halves: the tokens come from the site's own
    `capability_requirements` and the answer from plugin metadata, so nothing
    on this path knows what `geoip` is. What the mapping adds is *which*
    setting asked.
    """
    dns_only = load_plugins(
        ("blitzecdn.capabilities.dns.feature",), entry_point_group=None
    )
    requested = site(
        visitor_headers={"ip_country": True},
        firewall={"denied_countries": ["RU"]},
    )

    result = dns_only.bind_rendering((), context()).validate_site(requested)

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
        baseline.bind_rendering((), context())
        .validate_site(site(visitor_headers={"ip_country": True}))
        .blocking
    )

    assert "site's visitor_headers.ip_country requests it" in issue.message


# --- lifecycle --------------------------------------------------------------


def test_startup_and_shutdown_reach_every_feature_with_the_process_kind(baseline):
    seen: list[tuple[str, ProcessKind]] = []

    class Lifecycle:
        @hookimpl
        def blitzecdn_plugin_metadata(self) -> PluginMetadata:
            return PluginMetadata(name="lifecycle", version="1.0", api_version=2)

        @hookimpl
        def blitzecdn_startup(self, context) -> None:
            seen.append(("start", context.process))

        @hookimpl
        def blitzecdn_shutdown(self, context) -> None:
            seen.append(("stop", context.process))

    baseline._manager.register(Lifecycle(), name="lifecycle")
    built_in = FeatureOperations(
        plugin="jobs",
        startup=lambda process: seen.append(("built-in start", process)),
        shutdown=lambda process: seen.append(("built-in stop", process)),
    )
    operations = baseline.bind_operations((built_in,), context(ProcessKind.API))

    operations.startup()
    operations.shutdown()

    assert seen == [
        ("built-in start", ProcessKind.API),
        ("start", ProcessKind.API),
        ("stop", ProcessKind.API),
        ("built-in stop", ProcessKind.API),
    ]


def test_deployment_recovery_runs_at_api_startup_only(baseline):
    calls: list[str] = []

    for process in ProcessKind:
        baseline.bind_operations((_drift(calls),), context(process)).startup()

    assert calls == ["initialize"]


# --- scheduled jobs ---------------------------------------------------------


def test_two_plugins_cannot_contribute_one_job_name(baseline):
    class Clasher:
        @hookimpl
        def blitzecdn_plugin_metadata(self) -> PluginMetadata:
            return PluginMetadata(name="clasher", version="1.0", api_version=2)

        @hookimpl
        def blitzecdn_scheduled_jobs(self, context) -> list[ScheduledJob]:
            return [
                ScheduledJob(
                    plugin="clasher",
                    name="check-drift",
                    interval_seconds=60,
                    run=lambda _o: None,
                )
            ]

    baseline._manager.register(Clasher(), name="clasher")

    with pytest.raises(PluginError, match="'deployments' and 'clasher'"):
        baseline.bind_operations((_drift([]),), context())


def test_every_built_in_job_calls_the_service_that_owns_its_work(baseline):
    """A job is one object — its interval and its work — so both are checkable."""
    calls: list[str] = []
    jobs = baseline.bind_operations((_drift(calls),), context()).scheduled_jobs

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
        ).for_plugin("certificates")
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
        ),
        context(),
    )

    assert len(operations.scheduled_jobs) == 4
    assert all(job.interval_seconds == 0 for job in operations.scheduled_jobs.values())


# --- plugin metadata --------------------------------------------------------


def test_a_plugin_name_has_to_be_usable_as_an_identifier():
    with pytest.raises(ValueError, match="must be alphanumeric"):
        PluginMetadata(name="not a name!", version="1.0", api_version=2)


def test_a_registry_reports_what_is_installed_and_what_was_refused(baseline):
    found = attach(
        baseline, entry_point("waf", "external"), entry_point("broken", "broken")
    )
    registry = PluginRegistry(
        baseline.features,
        manager=baseline._manager,
        external=found.plugins,
        rejected=found.rejected,
    )

    assert "waf" in registry
    assert "dns" in registry
    assert [rejection.reason for rejection in registry.rejected] != []


def test_the_shipped_registry_loads_every_built_in_and_rejects_nothing():
    registry = load_control_plane_plugins(entry_point_group=None)

    assert {"geoip", "http3", "certificates", "tls"} <= {
        plugin.name for plugin in registry.plugins
    }
    assert registry.rejected == ()


# --- the contributions a plugin makes are values, not calls ------------------


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

    check = HealthCheck(plugin="waf", name="x", check=unhealthy)

    with pytest.raises(ConnectionError):
        check.check()


def test_a_command_group_names_the_subcommand_it_appears_under():
    group = CliCommandGroup(plugin="waf", name="waf", app=Typer())

    assert group.name == "waf"


def test_a_desired_state_hook_returning_the_wrong_shape_is_named(baseline):
    class Careless:
        @hookimpl
        def blitzecdn_plugin_metadata(self) -> PluginMetadata:
            return PluginMetadata(name="careless", version="1.0", api_version=2)

        @hookimpl
        def blitzecdn_site_desired_state(self, site: CdnSite) -> object:
            return {"waf_enabled": True}

    baseline._manager.register(Careless(), name="careless")

    with pytest.raises(PluginError, match="must return a SiteStateContribution"):
        baseline.bind_rendering((), context()).site_variables(site())


def test_a_plugin_whose_metadata_is_not_metadata_is_refused(baseline):
    """Answering the identity hook with something that is not an identity."""

    class Careless:
        @hookimpl
        def blitzecdn_plugin_metadata(self) -> object:
            return "waf 1.0"

    with pytest.raises(PluginError, match="must return a PluginMetadata, not str"):
        register(baseline._manager, Careless(), "careless")


def test_a_contribution_into_a_shared_namespace_says_who_contributed_it():
    """Registration under a name the whole fleet shares carries its owner.

    Five of these carried `plugin` and three did not, and the split was not a
    decision — `CliCommandGroup`, `HealthCheck` and `ScheduledJob` were simply
    the ones nobody had needed to attribute yet. The cost showed up three
    times: the duplicate-job error could say two plugins collided but not
    which two, two health checks could answer to one name with nothing to tell
    them apart, and the frozen CLI surface had to guess ownership from a
    callback's `__module__`.

    The rule is positional rather than a list to maintain: a contribution type
    whose instances are registered under a name that the rest of the fleet can
    also claim has to say which plugin claimed it. A new one that forgets fails
    here, while the field is still free to add — once a wheel exists that
    constructs one, adding a required field is a breaking change to its
    constructor.
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
            field.name == "plugin"
            and field.default is MISSING
            and field.default_factory is MISSING
            for field in fields(contribution)
        )
    ]
    assert missing == []


def test_two_plugins_cannot_answer_to_one_health_check_name(baseline):
    """`/health` reports a check by name, so the name has to be one plugin's."""

    class Clasher:
        @hookimpl
        def blitzecdn_plugin_metadata(self) -> PluginMetadata:
            return PluginMetadata(name="clasher", version="1.0", api_version=2)

        @hookimpl
        def blitzecdn_health_checks(self, context) -> list[HealthCheck]:
            return [HealthCheck(plugin="clasher", name="database", check=lambda: None)]

    baseline._manager.register(Clasher(), name="clasher")

    with pytest.raises(PluginError, match="'diagnostics' and 'clasher'"):
        baseline.bind_operations(
            (
                diagnostics_operations(
                    read_database=lambda: None, queue_ready=lambda: True
                ),
            ),
            context(),
        )
