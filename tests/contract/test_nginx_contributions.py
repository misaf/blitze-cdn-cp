"""The static resource seam optional Nginx implementations cross."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from blitzecdn.core.exceptions import ConfigurationError, PluginError
from blitzecdn.core.plugins import (
    CapabilitySetting,
    EnvironmentKey,
    NginxContribution,
    load_plugins,
)
from blitzecdn.core.plugins.resolution import (
    ConfigurationContribution,
    resolve_capability_environment,
    resolve_nginx_resources,
)


def _template(root: Path, name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text("# fragment\n", encoding="utf-8")


def test_nginx_resources_are_deterministic_and_keep_contexts_stable(tmp_path):
    alpha = tmp_path / "alpha"
    zulu = tmp_path / "zulu"
    _template(alpha, "alpha-server.conf.j2")
    _template(zulu, "zulu-upstream.conf.j2")
    contributions = [
        NginxContribution(
            plugin="zulu",
            templates_path=zulu,
            upstream_fragments=("zulu-upstream.conf.j2",),
        ),
        NginxContribution(
            plugin="alpha",
            templates_path=alpha,
            server_fragments=("alpha-server.conf.j2",),
        ),
    ]

    first = resolve_nginx_resources(contributions)
    second = resolve_nginx_resources(reversed(contributions))

    assert first == second
    assert [resource.plugin for resource in first["server"]] == ["alpha"]
    assert [resource.plugin for resource in first["upstream"]] == ["zulu"]


def test_two_plugins_cannot_own_one_nginx_resource(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _template(first, "shared.conf.j2")
    _template(second, "shared.conf.j2")

    with pytest.raises(PluginError, match=r"shared.*alpha.*zulu"):
        resolve_nginx_resources(
            [
                NginxContribution("alpha", first, server_fragments=("shared.conf.j2",)),
                NginxContribution(
                    "zulu", second, upstream_fragments=("shared.conf.j2",)
                ),
            ]
        )


def test_a_declared_nginx_resource_must_ship_in_the_package(tmp_path):
    with pytest.raises(PluginError, match=r"missing.conf.j2.*is not a file"):
        resolve_nginx_resources(
            [
                NginxContribution(
                    "broken", tmp_path, server_fragments=("missing.conf.j2",)
                )
            ]
        )


def test_installed_packages_supply_only_package_owned_templates():
    """Whatever is attached, every Nginx fragment comes out of a package.

    Derived from what is installed rather than compared against a list of
    names: this suite runs both with every optional distribution attached and
    with none of them, and a fixed list would be wrong in one of those. The
    invariant is the same either way — core contributes no fragment of its own,
    and every fragment that exists ships inside the distribution that owns it.
    """
    plugins = load_plugins()
    optional = {plugin.name for plugin in plugins.plugins if not plugin.required}
    resources = resolve_nginx_resources(plugins.nginx_contributions())
    flattened = [resource for context in resources.values() for resource in context]

    assert {resource.plugin for resource in flattened} <= optional
    assert all(
        "/packages/blitzecdn-" in str(resource.template) for resource in flattened
    )


def test_only_explicitly_claimed_environment_reaches_ansible(tmp_path):
    roles = tmp_path / "roles"
    roles.mkdir()
    contribution = ConfigurationContribution(
        plugin="geoip",
        environment_keys=(EnvironmentKey(name="BLITZE_MAXMIND_LICENSE_KEY"),),
    )
    configured = {"BLITZE_MAXMIND_LICENSE_KEY": SecretStr("sentinel")}

    resolved = resolve_capability_environment([contribution], configured, {}, tmp_path)

    assert resolved.environment == configured


def test_a_package_reads_its_own_configuration_and_no_other_package_s(tmp_path):
    """The scoped half of the same resolution, which is what a package reads.

    `capability_environment` used to be handed to a package whole, so a
    capability that wanted its own credential was given every other
    capability's as well and picked its own out by name. Here `geoip` cannot
    reach the token beside it, and the key it did not declare is refused rather
    than returned empty — the two mistakes look identical at the call site and
    only one of them is a typo.
    """
    roles = tmp_path / "roles"
    roles.mkdir()
    contributions = [
        ConfigurationContribution(
            "geoip",
            environment_keys=(EnvironmentKey(name="BLITZE_MAXMIND_LICENSE_KEY"),),
        ),
        ConfigurationContribution(
            "security", environment_keys=(EnvironmentKey(name="BLITZE_TOKEN"),)
        ),
    ]
    configured = {
        "BLITZE_MAXMIND_LICENSE_KEY": SecretStr("sentinel"),
        "BLITZE_TOKEN": SecretStr("other"),
    }

    resolved = resolve_capability_environment(contributions, configured, {}, tmp_path)
    geoip = resolved.for_plugin("geoip")

    assert geoip.secret("BLITZE_MAXMIND_LICENSE_KEY").get_secret_value() == "sentinel"
    assert geoip.is_set("BLITZE_MAXMIND_LICENSE_KEY")
    with pytest.raises(PluginError, match=r"geoip.*BLITZE_TOKEN.*does not declare"):
        geoip.secret("BLITZE_TOKEN")


def test_a_declared_key_is_present_and_empty_rather_than_absent(tmp_path):
    """An unset key is a state, not a missing entry.

    A package that declared a key always gets it back, so reading one is never
    a `.get(..., "")` against a dictionary that may or may not have heard of
    it, and "the operator set nothing" stays distinguishable from "this
    package never claimed the name".
    """
    roles = tmp_path / "roles"
    roles.mkdir()
    contribution = ConfigurationContribution(
        "security", environment_keys=(EnvironmentKey(name="BLITZE_TOKEN"),)
    )

    config = resolve_capability_environment(
        [contribution], {}, {}, tmp_path
    ).for_plugin("security")

    assert config.secret("BLITZE_TOKEN").get_secret_value() == ""
    assert not config.is_set("BLITZE_TOKEN")


def test_a_plugin_that_declares_nothing_gets_an_empty_configuration(tmp_path):
    roles = tmp_path / "roles"
    roles.mkdir()
    contribution = ConfigurationContribution("resolver")

    resolved = resolve_capability_environment([contribution], {}, {}, tmp_path)

    assert resolved.for_plugin("resolver").values == {}


def test_a_required_key_that_is_unset_stops_the_control_plane(tmp_path):
    """Named by capability and by key, at composition, before anything runs."""
    roles = tmp_path / "roles"
    roles.mkdir()
    contribution = ConfigurationContribution(
        "widget",
        environment_keys=(
            EnvironmentKey(
                name="BLITZE_WIDGET_TOKEN",
                required=True,
                summary="Issued in the widget console.",
            ),
        ),
    )

    with pytest.raises(
        ConfigurationError, match=r"widget.*BLITZE_WIDGET_TOKEN.*widget console"
    ):
        resolve_capability_environment([contribution], {}, {}, tmp_path)


def test_a_value_shorter_than_the_capability_declared_is_refused(tmp_path):
    """The placeholder case, refused where it is cheap.

    A signing secret somebody meant to replace used to start a controller, be
    forwarded to every play, and be reported only by the first site that turned
    the capability on. The length is a rule core can hold without knowing what the
    value means, so it holds it.
    """
    roles = tmp_path / "roles"
    roles.mkdir()
    contribution = ConfigurationContribution(
        "security",
        environment_keys=(EnvironmentKey(name="BLITZE_TOKEN", minimum_bytes=32),),
    )

    with pytest.raises(ConfigurationError, match=r"BLITZE_TOKEN.*32 bytes.*security"):
        resolve_capability_environment(
            [contribution], {"BLITZE_TOKEN": SecretStr("x")}, {}, tmp_path
        )

    resolved = resolve_capability_environment(
        [contribution], {"BLITZE_TOKEN": SecretStr("x" * 32)}, {}, tmp_path
    )

    assert resolved.for_plugin("security").is_set("BLITZE_TOKEN")


def test_a_minimum_length_says_nothing_about_whether_a_key_is_required(tmp_path):
    """The two rules are independent, and Under Attack Mode needs them to be.

    A controller with no signing secret is a working controller with one site
    setting it will refuse. A controller with a four-character one is a
    mistake. Only the second is fatal.
    """
    roles = tmp_path / "roles"
    roles.mkdir()
    contribution = ConfigurationContribution(
        "security",
        environment_keys=(EnvironmentKey(name="BLITZE_TOKEN", minimum_bytes=32),),
    )

    resolved = resolve_capability_environment([contribution], {}, {}, tmp_path)

    assert not resolved.for_plugin("security").is_set("BLITZE_TOKEN")


def test_duplicate_environment_ownership_names_both_plugins(tmp_path):
    roles = tmp_path / "roles"
    roles.mkdir()
    contributions = [
        ConfigurationContribution(
            "alpha", environment_keys=(EnvironmentKey(name="BLITZE_TOKEN"),)
        ),
        ConfigurationContribution(
            "zulu", environment_keys=(EnvironmentKey(name="BLITZE_TOKEN"),)
        ),
    ]

    with pytest.raises(PluginError, match=r"BLITZE_TOKEN.*alpha.*zulu"):
        resolve_capability_environment(contributions, {}, {}, tmp_path)


def test_unknown_capability_environment_is_a_configuration_error(tmp_path):
    with pytest.raises(PluginError, match=r"unknown.*BLITZE_TYPO"):
        resolve_capability_environment(
            [], {"BLITZE_TYPO": SecretStr("value")}, {}, tmp_path
        )


# --- non-secret capability settings -----------------------------------------


def _setting(**overrides) -> ConfigurationContribution:
    return ConfigurationContribution(
        plugin="certificates",
        settings=(CapabilitySetting(**overrides),),
    )


def test_a_setting_a_controller_never_configured_is_its_declared_default(tmp_path):
    """Which is the whole difference between a setting and a secret.

    A secret is asked whether it is *set*, because core cannot supply one. A
    setting always has a value, so no package carries an "or the default"
    expression at every read — the declaration is the default.
    """
    contribution = _setting(name="BLITZE_RENEWAL", default=43_200)

    resolved = resolve_capability_environment([contribution], {}, {}, tmp_path)

    assert resolved.for_plugin("certificates").integer("BLITZE_RENEWAL") == 43_200


def test_a_setting_reads_as_the_type_its_default_fixed(tmp_path):
    """The default carries the type as well as the value, so the two agree."""
    contributions = [
        ConfigurationContribution(
            plugin="example",
            settings=(
                CapabilitySetting(name="BLITZE_COUNT", default=1),
                CapabilitySetting(name="BLITZE_FLAG", default=False),
                CapabilitySetting(name="BLITZE_NAME", default=""),
            ),
        )
    ]
    configured = {
        "BLITZE_COUNT": SecretStr("7"),
        "BLITZE_FLAG": SecretStr("yes"),
        "BLITZE_NAME": SecretStr("certbot"),
    }

    config = resolve_capability_environment(
        contributions, configured, {}, tmp_path
    ).for_plugin("example")

    assert config.integer("BLITZE_COUNT") == 7
    assert config.flag("BLITZE_FLAG") is True
    assert config.text("BLITZE_NAME") == "certbot"


def test_a_word_is_never_read_as_a_flag(tmp_path):
    """`bool("false")` is `True`, which is how a disabled switch turns itself on."""
    contribution = ConfigurationContribution(
        plugin="example",
        settings=(CapabilitySetting(name="BLITZE_FLAG", default=True),),
    )

    config = resolve_capability_environment(
        [contribution], {"BLITZE_FLAG": SecretStr("false")}, {}, tmp_path
    ).for_plugin("example")

    assert config.flag("BLITZE_FLAG") is False
    with pytest.raises(ConfigurationError, match=r"BLITZE_FLAG.*'perhaps'.*example"):
        resolve_capability_environment(
            [contribution], {"BLITZE_FLAG": SecretStr("perhaps")}, {}, tmp_path
        )


def test_a_value_outside_the_declared_bounds_names_the_capability(tmp_path):
    contribution = _setting(
        name="BLITZE_RENEWAL", default=600, minimum=0, maximum=86_400
    )

    with pytest.raises(ConfigurationError, match=r"BLITZE_RENEWAL.*certificates"):
        resolve_capability_environment(
            [contribution], {"BLITZE_RENEWAL": SecretStr("99999999")}, {}, tmp_path
        )


def test_a_relative_path_setting_resolves_under_this_controllers_state(tmp_path):
    """A capability names a location before it knows where the state dir is."""
    contribution = _setting(name="BLITZE_ARCHIVES", default=Path("backups"))

    config = resolve_capability_environment(
        [contribution], {}, {}, tmp_path
    ).for_plugin("certificates")

    assert config.path("BLITZE_ARCHIVES") == tmp_path / "backups"


def test_an_absolute_path_setting_is_taken_as_written(tmp_path):
    contribution = _setting(name="BLITZE_ARCHIVES", default=Path("backups"))

    config = resolve_capability_environment(
        [contribution],
        {"BLITZE_ARCHIVES": SecretStr("/var/backups/blitzecdn")},
        {},
        tmp_path,
    ).for_plugin("certificates")

    assert config.path("BLITZE_ARCHIVES") == Path("/var/backups/blitzecdn")


def test_reading_a_setting_as_the_wrong_type_is_refused_by_name(tmp_path):
    """The declaration is held to the call site, not only the value to it."""
    contribution = _setting(name="BLITZE_RENEWAL", default=600)

    config = resolve_capability_environment(
        [contribution], {}, {}, tmp_path
    ).for_plugin("certificates")

    with pytest.raises(PluginError, match=r"BLITZE_RENEWAL.*str.*int"):
        config.text("BLITZE_RENEWAL")


def test_a_setting_may_come_from_the_committed_file(tmp_path):
    contribution = _setting(name="BLITZE_RENEWAL", default=600)

    config = resolve_capability_environment(
        [contribution], {}, {"BLITZE_RENEWAL": "7200"}, tmp_path
    ).for_plugin("certificates")

    assert config.integer("BLITZE_RENEWAL") == 7200


def test_the_environment_outranks_the_committed_file(tmp_path):
    contribution = _setting(name="BLITZE_RENEWAL", default=600)

    config = resolve_capability_environment(
        [contribution],
        {"BLITZE_RENEWAL": SecretStr("99")},
        {"BLITZE_RENEWAL": "7200"},
        tmp_path,
    ).for_plugin("certificates")

    assert config.integer("BLITZE_RENEWAL") == 99


def test_a_secret_may_never_be_set_in_the_committed_file(tmp_path):
    """`.env` is 0600 and uncommitted; `blitzecdn.toml` is neither.

    A capability's non-secret settings belong in the committed file, which is
    why keys core does not recognise are staged from it at all. A *secret*
    must not be, however a package documents it — so the two origins stay
    apart and a declared `EnvironmentKey` appearing in the file is refused
    with the spelling an operator used.
    """
    contribution = ConfigurationContribution(
        plugin="security",
        environment_keys=(EnvironmentKey(name="BLITZE_UNDER_ATTACK_SECRET"),),
    )

    with pytest.raises(ConfigurationError, match=r"secrets.*under_attack_secret"):
        resolve_capability_environment(
            [contribution], {}, {"BLITZE_UNDER_ATTACK_SECRET": "s" * 32}, tmp_path
        )


def test_a_setting_is_not_forwarded_to_ansible(tmp_path):
    """Only secrets reach the subprocess; a setting is the controller's.

    A setting has a resolved value whether or not an operator supplied one, so
    forwarding the staged strings would send Ansible whichever subset happened
    to be set rather than the answer. A role that needs the value reads it
    from desired state or from its own defaults.
    """
    contributions = [
        ConfigurationContribution(
            plugin="example",
            environment_keys=(EnvironmentKey(name="BLITZE_TOKEN"),),
            settings=(CapabilitySetting(name="BLITZE_COUNT", default=1),),
        )
    ]
    configured = {"BLITZE_TOKEN": SecretStr("x"), "BLITZE_COUNT": SecretStr("7")}

    resolved = resolve_capability_environment(contributions, configured, {}, tmp_path)

    assert set(resolved.environment) == {"BLITZE_TOKEN"}


def test_one_name_may_not_be_both_a_secret_and_a_setting(tmp_path):
    """They share a namespace, so this is the same collision as any other."""
    contributions = [
        ConfigurationContribution(
            plugin="alpha", environment_keys=(EnvironmentKey(name="BLITZE_SHARED"),)
        ),
        ConfigurationContribution(
            plugin="zulu",
            settings=(CapabilitySetting(name="BLITZE_SHARED", default=1),),
        ),
    ]

    with pytest.raises(PluginError, match=r"BLITZE_SHARED.*alpha.*zulu"):
        resolve_capability_environment(contributions, {}, {}, tmp_path)


def test_a_setting_may_not_declare_bounds_on_something_uncountable():
    with pytest.raises(ValueError, match=r"BLITZE_NAME.*whole numbers only"):
        CapabilitySetting(name="BLITZE_NAME", default="certbot", minimum=1)


def test_a_default_outside_its_own_bounds_is_refused():
    with pytest.raises(ValueError, match=r"BLITZE_COUNT.*minimum"):
        CapabilitySetting(name="BLITZE_COUNT", default=0, minimum=30)
