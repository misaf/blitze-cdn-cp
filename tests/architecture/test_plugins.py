"""The extension mechanism: discovery, registration, and what it refuses.

Two things are being held here. The first is that the built-in features really
do reach the API, the CLI, the scheduler and the desired-state document through
plugin registration and not through a list somebody maintains. The second is
that a package this repository has never heard of can do the same — so most of
these tests load `tests/fixtures/plugins/*` through the real entry-point
machinery rather than registering an object directly, because the path an
operator's `pip install` takes is the path worth testing.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint
from types import SimpleNamespace

import pytest
from fastapi import APIRouter
from typer import Typer

from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.plugins import (
    BUILTIN_PLUGINS,
    ENTRY_POINT_GROUP,
    CliCommandGroup,
    FleetStateContribution,
    HealthCheck,
    PluginMetadata,
    PluginRegistry,
    ProcessKind,
    RuntimeContext,
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
from blitzecdn.features.sites.domain import CdnSite

_FIXTURES = "external_plugins"


def entry_point(name: str, module: str) -> EntryPoint:
    """One entry point, exactly as an installed distribution would declare it."""
    return EntryPoint(name=name, value=f"{_FIXTURES}.{module}", group=ENTRY_POINT_GROUP)


@pytest.fixture
def builtins() -> PluginRegistry:
    """The features this distribution ships, and nothing an environment added.

    `entry_point_group=None` on purpose: a test asserting on the built-in
    feature set must not change its answer because a developer happens to have
    an unrelated BlitzeCDN plugin installed in the same virtualenv.
    """
    return load_plugins(entry_point_group=None)


@pytest.fixture
def platform() -> SimpleNamespace:
    """Enough of a control plane for a plugin to register against.

    Registration reads intervals and holds on to services; it calls none of
    them, which is the property that makes a plugin cheap to construct and this
    double honest.
    """
    return SimpleNamespace(
        settings=SimpleNamespace(
            certificate_reconcile_interval_seconds=3600,
            certificate_renewal_interval_seconds=3600,
            certificate_renewal_budget_seconds=300,
            ssl_automatic_scan_interval_seconds=86_400,
            drift_check_interval_seconds=900,
        ),
    )


def site(**overrides: object) -> CdnSite:
    return CdnSite.model_validate(
        {
            "name": "cdn-example-com",
            "server_names": ["cdn.example.com"],
            "origin_host": "198.51.100.10",
            "compression": "off",
            **overrides,
        }
    )


# --- the hook specifications ------------------------------------------------


def test_the_hook_contract_is_small_and_every_hook_is_a_registration_point():
    """A hook is somewhere core has to be *told* something exists.

    The list is asserted whole rather than counted, because the failure this
    guards is not "there are too many" but "someone added
    `blitzecdn_issue_certificate`" — a business call wearing a hook's clothes,
    which would give one caller an unnamed implementation and no return value.
    """
    manager = build_plugin_manager()
    assert set(manager.hook.__dict__) - {"_needs_discovery"} == {
        "blitzecdn_plugin_metadata",
        "blitzecdn_api_routers",
        "blitzecdn_cli_commands",
        "blitzecdn_health_checks",
        "blitzecdn_scheduled_jobs",
        "blitzecdn_site_desired_state",
        "blitzecdn_fleet_desired_state",
        "blitzecdn_deployment_checks",
        "blitzecdn_startup",
        "blitzecdn_shutdown",
    }


# --- built-in discovery -----------------------------------------------------


def test_every_built_in_feature_registers_and_declares_itself_required(builtins):
    assert {plugin.name for plugin in builtins.plugins} == {
        path.removeprefix("blitzecdn.features.").removesuffix(".plugin")
        for path in BUILTIN_PLUGINS
    }
    assert all(plugin.required for plugin in builtins.plugins)
    assert builtins.rejected == ()


def test_a_strategy_or_mode_never_registers_as_a_plugin(builtins):
    """The capability registers; the option inside it does not.

    `http3` was a plugin once. So were `certificates` and `automatic_ssl`,
    which is how one capability came to have two registrations and two owners
    of `SslMode`. Each name here would be a plausible package, and each belongs
    inside one that already exists.
    """
    for capability in ("http", "tls", "sites"):
        assert capability in builtins
        assert f"blitzecdn.features.{capability}.plugin" in BUILTIN_PLUGINS

    for capability in ("compression", "security"):
        assert capability not in builtins
        assert f"blitzecdn.features.{capability}.plugin" not in BUILTIN_PLUGINS

    for option in (
        "gzip",
        "brotli",
        "http3",
        "under_attack",
        "certificates",
        "automatic_ssl",
        "geoip",
    ):
        assert option not in builtins
        assert f"blitzecdn.features.{option}.plugin" not in BUILTIN_PLUGINS


def test_a_built_in_that_cannot_be_imported_stops_the_process():
    """No degraded mode: a control plane missing `dns` renders an empty fleet."""
    with pytest.raises(PluginError, match="could not be imported"):
        load_plugins(builtins=("blitzecdn.features.nonexistent.plugin",))


def test_a_built_in_that_declares_itself_optional_is_refused():
    with pytest.raises(PluginError, match="declares itself optional"):
        load_plugins(
            builtins=(f"{_FIXTURES}.external",),
            entry_point_group=None,
        )


def test_the_built_in_registration_order_does_not_change_desired_state(builtins):
    """Reordering `BUILTIN_PLUGINS` is a presentation change and nothing more."""
    reversed_order = load_plugins(
        builtins=tuple(reversed(BUILTIN_PLUGINS)), entry_point_group=None
    )
    platform = SimpleNamespace(certificates=None)
    subject = site()
    assert reversed_order.site_variables(subject, platform) == builtins.site_variables(
        subject, platform
    )


# --- external discovery through entry points --------------------------------


def test_an_external_plugin_is_discovered_through_its_entry_point(builtins):
    found = register_external(
        builtins._manager, points=[entry_point("waf", "external")]
    )

    assert [plugin.name for plugin in found.plugins] == ["waf"]
    assert found.rejected == ()
    assert "/v1/waf/status" in {
        route.path for router in builtins.api_routers() for route in router.routes
    }
    assert "waf" in {group.name for group in builtins.cli_commands()}


def test_an_external_plugin_contributes_jobs_health_checks_and_state(
    builtins, platform
):
    register_external(builtins._manager, points=[entry_point("waf", "external")])

    assert "waf-rule-refresh" in builtins.scheduled_jobs(platform)
    assert "waf-rules" in {check.name for check in builtins.health_checks(platform)}
    assert builtins.site_variables(site(), platform)["waf_mode"] == "enforce"
    assert builtins.fleet_variables((site(),), platform)["blitzecdn_edge_waf_enabled"]


def test_core_needs_no_change_for_an_external_feature(builtins):
    """The acceptance criterion, stated as a diff that is not required.

    Everything the WAF plugin adds — a route, a command, a job, a check, its
    share of every site's desired state — arrives without `BUILTIN_PLUGINS`,
    `api/app.py`, `cli/main.py`, `scheduler.py` or the renderer knowing it
    exists.
    """
    before = len(builtins.api_routers())
    register_external(builtins._manager, points=[entry_point("waf", "external")])

    assert len(builtins.api_routers()) == before + 1
    assert f"{_FIXTURES}.external" not in BUILTIN_PLUGINS


# --- the failure policy -----------------------------------------------------


def test_an_external_plugin_that_raises_on_import_is_skipped_with_its_reason(
    builtins, caplog
):
    """Skipped, never silent: the reason is logged *and* kept for an operator."""
    with caplog.at_level("WARNING"):
        found = register_external(
            builtins._manager, points=[entry_point("waf", "broken")]
        )

    assert found.plugins == ()
    assert [rejection.source for rejection in found.rejected] == [
        f"waf ({_FIXTURES}.broken)"
    ]
    assert "this plugin is broken" in str(found.rejected[0])
    assert "this plugin is broken" in caplog.text


def test_a_plugin_that_will_not_say_who_it_is_contributes_nothing(builtins):
    """Anonymous contributions cannot be attributed, so they are not accepted."""
    found = register_external(
        builtins._manager, points=[entry_point("anonymous", "anonymous")]
    )

    assert "no blitzecdn_plugin_metadata hook" in str(found.rejected[0])
    assert "anonymous" not in {
        check.name for check in builtins.health_checks(SimpleNamespace())
    }


def test_a_plugin_from_a_later_hook_contract_is_refused_by_version(builtins):
    found = register_external(
        builtins._manager, points=[entry_point("future", "from_the_future")]
    )

    assert found.plugins == ()
    assert "targets hook API v99" in str(found.rejected[0])


def test_two_plugins_cannot_claim_one_name(builtins, platform):
    """A duplicate makes every later diagnostic ambiguous, so it is refused.

    Built-ins register first, so the impostor collides with `dns` rather than
    displacing it — an installed package must not be able to take a built-in
    feature's identity and quietly answer for it.
    """
    found = register_external(
        builtins._manager, points=[entry_point("impostor", "impostor")]
    )

    assert found.plugins == ()
    assert "already registered" in str(found.rejected[0])
    assert "dns" in builtins


def test_two_installed_packages_cannot_claim_one_name(builtins):
    """The same rule between two *optional* distributions, neither built in.

    Worth its own case because the collision an operator actually hits is this
    one: an optional capability was extracted into `blitzecdn-cache`, and a
    third-party package that also calls itself `cache` is now installable
    beside it. Neither has a built-in to lose to, so nothing about the
    registration order decides it — the second is refused with its source, and
    the first keeps the name and keeps working.
    """
    first = register_external(
        builtins._manager, points=[entry_point("waf", "external")]
    )
    second = register_external(
        builtins._manager, points=[entry_point("waf-again", "external")]
    )

    assert [plugin.name for plugin in first.plugins] == ["waf"]
    assert second.plugins == ()
    assert "already registered" in str(second.rejected[0])
    assert "waf-again" in str(second.rejected[0])


def test_a_capability_token_says_what_a_configuration_may_depend_on(builtins):
    """`provides` is separate from `name`, and defaults to it.

    A plugin identifies itself with `name`; a *configuration* depends on a
    capability. Almost always they are the same word, so `provides` defaults to
    empty and `capabilities` folds the name in — but the two are allowed to
    differ, which is what lets a replacement implementation answer for a token
    another package used to supply, under its own name.
    """
    assert "sites" in builtins.capabilities
    assert builtins.missing(["sites", "waf"]) == ("waf",)

    register_external(builtins._manager, points=[entry_point("waf", "external")])
    metadata = PluginMetadata(
        name="acme-waf", version="1.0", provides=frozenset({"waf", "ratelimit"})
    )
    assert metadata.capabilities == {"acme-waf", "waf", "ratelimit"}


def test_requiring_a_capability_nothing_provides_names_it(builtins):
    """The message an operator reads when a package they need is not installed.

    It has to name the missing token *and* what is installed: "backup is not
    available" without the second half leaves an operator unable to tell a
    typo from an uninstalled package.
    """
    builtins.require(["sites"], subject="a test")

    with pytest.raises(PluginError) as failure:
        builtins.require(["sites", "waf"], subject="this installation")

    message = str(failure.value)
    assert "waf" in message
    assert "this installation" in message
    assert "sites" in message
    assert "compression" not in message.split("Installed capabilities:")[0]


def test_one_broken_plugin_does_not_stop_the_others(builtins):
    found = register_external(
        builtins._manager,
        points=[entry_point("broken", "broken"), entry_point("waf", "external")],
    )

    assert [plugin.name for plugin in found.plugins] == ["waf"]
    assert len(found.rejected) == 1


def test_a_plugin_returning_the_wrong_shape_is_named_rather_than_crashing(builtins):
    class Careless:
        @hookimpl
        def blitzecdn_plugin_metadata(self) -> PluginMetadata:
            return PluginMetadata(name="careless", version="1.0")

        @hookimpl
        def blitzecdn_api_routers(self) -> object:
            return ["not a router"]

    builtins._manager.register(Careless(), name="careless")

    with pytest.raises(PluginError, match="blitzecdn_api_routers returned a str"):
        builtins.api_routers()


# --- ordering ---------------------------------------------------------------


def test_contributions_come_back_in_registration_order(builtins):
    """Pluggy calls implementations last-registered-first; operators read the
    other way round, and the published route list should not depend on that."""
    register_external(builtins._manager, points=[entry_point("waf", "external")])
    names = [group.name for group in builtins.cli_commands()]

    assert names.index("site") < names.index("edge") < names.index("waf")


# --- merging desired state --------------------------------------------------


def test_a_declared_override_wins_wherever_it_registered():
    base = SiteStateContribution(
        plugin="sites", variables={"certificate_path": "/model"}
    )
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
        FleetStateContribution(plugin="sites", variables={"a": True}),
        FleetStateContribution(plugin="waf", variables={"b": False}),
    ]

    assert merge_variables(contributions, subject="fleet") == {"a": True, "b": False}


# --- deployment checks ------------------------------------------------------


def test_deployment_checks_are_collected_from_every_plugin(builtins):
    class Objector:
        @hookimpl
        def blitzecdn_plugin_metadata(self) -> PluginMetadata:
            return PluginMetadata(name="objector", version="1.0")

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

    builtins._manager.register(Objector(), name="objector")
    result = builtins.validate_site(site(), SimpleNamespace())

    assert not result.ok
    assert [issue.message for issue in result.blocking] == ["no rule set loaded"]
    assert len(result.issues) == 2


def test_a_site_no_plugin_objects_to_is_deployable(builtins):
    assert builtins.validate_site(site(), SimpleNamespace()).ok


# --- lifecycle --------------------------------------------------------------


def test_startup_and_shutdown_reach_every_plugin_with_the_process_kind():
    seen: list[tuple[str, ProcessKind]] = []

    class Lifecycle:
        @hookimpl
        def blitzecdn_plugin_metadata(self) -> PluginMetadata:
            return PluginMetadata(name="lifecycle", version="1.0")

        @hookimpl
        def blitzecdn_startup(self, context: RuntimeContext) -> None:
            seen.append(("start", context.process))

        @hookimpl
        def blitzecdn_shutdown(self, context: RuntimeContext) -> None:
            seen.append(("stop", context.process))

    manager = build_plugin_manager()
    manager.register(Lifecycle(), name="lifecycle")
    registry = PluginRegistry(manager)
    context = RuntimeContext(process=ProcessKind.WORKER, settings=object())

    registry.startup(context, SimpleNamespace())
    registry.shutdown(context, SimpleNamespace())

    assert seen == [("start", ProcessKind.WORKER), ("stop", ProcessKind.WORKER)]


# --- scheduled jobs ---------------------------------------------------------


def test_two_plugins_cannot_contribute_one_job_name(builtins, platform):
    class Clasher:
        @hookimpl
        def blitzecdn_plugin_metadata(self) -> PluginMetadata:
            return PluginMetadata(name="clasher", version="1.0")

        @hookimpl
        def blitzecdn_scheduled_jobs(self, platform: object) -> list[ScheduledJob]:
            return [
                ScheduledJob(
                    name="check-drift", interval_seconds=60, run=lambda _o: None
                )
            ]

    builtins._manager.register(Clasher(), name="clasher")

    with pytest.raises(PluginError, match="job named 'check-drift'"):
        builtins.scheduled_jobs(platform)


# --- plugin metadata --------------------------------------------------------


def test_a_plugin_name_has_to_be_usable_as_an_identifier():
    with pytest.raises(ValueError, match="must be alphanumeric"):
        PluginMetadata(name="not a name!", version="1.0")


def test_a_registry_reports_what_is_installed_and_what_was_refused(builtins):
    found = register_external(
        builtins._manager,
        points=[entry_point("waf", "external"), entry_point("broken", "broken")],
    )
    registry = PluginRegistry(
        builtins._manager,
        plugins=(*builtins.plugins, *found.plugins),
        rejected=found.rejected,
    )

    assert "waf" in registry
    assert "dns" in registry
    assert [rejection.reason for rejection in registry.rejected] != []


# --- the contributions a plugin makes are values, not calls ------------------


def test_a_contributed_group_may_carry_root_level_verbs(builtins):
    """`deploy` is a verb an operator types, not a noun to nest it under."""
    rootless = [group for group in builtins.cli_commands() if group.name is None]

    assert rootless
    assert all(isinstance(group.app, Typer) for group in rootless)


def test_routers_are_real_routers(builtins):
    assert all(isinstance(router, APIRouter) for router in builtins.api_routers())


def test_a_health_check_fails_by_raising():
    def unhealthy() -> None:
        raise ConnectionError("no")

    check = HealthCheck(name="x", check=unhealthy)

    with pytest.raises(ConnectionError):
        check.check()


def test_a_command_group_names_the_subcommand_it_appears_under():
    group = CliCommandGroup(name="waf", app=Typer())

    assert group.name == "waf"


def test_every_built_in_job_calls_the_service_that_owns_its_work(builtins):
    """A job is one object — its interval and its work — so both are checkable.

    The failure this replaces was structural: an interval in `scheduler.py` and
    a matching actor in `worker.py`, kept in step by hand, where renaming one
    published into a queue nothing consumed.
    """
    calls: list[str] = []
    platform = SimpleNamespace(
        settings=SimpleNamespace(drift_check_interval_seconds=900),
        deployments=SimpleNamespace(check_drift=lambda _o: calls.append("drift")),
    )
    jobs = builtins.scheduled_jobs(platform)

    assert set(jobs) == {"check-drift"}
    for name in sorted(jobs):
        jobs[name].run("scheduler")

    assert calls == ["drift"]
    assert jobs["check-drift"].interval_seconds == 900


def test_a_disabled_interval_is_how_a_job_is_turned_off(builtins):
    platform = SimpleNamespace(
        settings=SimpleNamespace(
            certificate_reconcile_interval_seconds=0,
            certificate_renewal_interval_seconds=0,
            certificate_renewal_budget_seconds=300,
            ssl_automatic_scan_interval_seconds=0,
            drift_check_interval_seconds=0,
        )
    )

    assert all(
        job.interval_seconds == 0 for job in builtins.scheduled_jobs(platform).values()
    )


def test_a_desired_state_hook_returning_the_wrong_shape_is_named(builtins, platform):
    class Careless:
        @hookimpl
        def blitzecdn_plugin_metadata(self) -> PluginMetadata:
            return PluginMetadata(name="careless", version="1.0")

        @hookimpl
        def blitzecdn_site_desired_state(self, site: CdnSite) -> object:
            return {"waf_enabled": True}

    builtins._manager.register(Careless(), name="careless")

    with pytest.raises(PluginError, match="must return a SiteStateContribution"):
        builtins.site_variables(site(), platform)


def test_a_plugin_whose_metadata_is_not_metadata_is_refused(builtins):
    """Answering the identity hook with something that is not an identity."""

    class Careless:
        @hookimpl
        def blitzecdn_plugin_metadata(self) -> object:
            return "waf 1.0"

    with pytest.raises(PluginError, match="must return a PluginMetadata, not str"):
        register(builtins._manager, Careless(), "careless")
