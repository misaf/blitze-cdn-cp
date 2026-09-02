"""The static resource seam optional Nginx implementations cross."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from blitzecdn.core.exceptions import ConfigurationError, PluginError
from blitzecdn.core.plugins import (
    AnsibleContribution,
    EnvironmentKey,
    NginxContribution,
    load_plugins,
)
from blitzecdn.core.plugins.resolution import (
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
    contribution = AnsibleContribution(
        plugin="geoip",
        roles_path=roles,
        environment_keys=(EnvironmentKey(name="BLITZE_MAXMIND_LICENSE_KEY"),),
    )
    configured = {"BLITZE_MAXMIND_LICENSE_KEY": SecretStr("sentinel")}

    resolved = resolve_capability_environment([contribution], configured)

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
        AnsibleContribution(
            "geoip",
            roles,
            environment_keys=(EnvironmentKey(name="BLITZE_MAXMIND_LICENSE_KEY"),),
        ),
        AnsibleContribution(
            "security", roles, environment_keys=(EnvironmentKey(name="BLITZE_TOKEN"),)
        ),
    ]
    configured = {
        "BLITZE_MAXMIND_LICENSE_KEY": SecretStr("sentinel"),
        "BLITZE_TOKEN": SecretStr("other"),
    }

    resolved = resolve_capability_environment(contributions, configured)
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
    contribution = AnsibleContribution(
        "security", roles, environment_keys=(EnvironmentKey(name="BLITZE_TOKEN"),)
    )

    config = resolve_capability_environment([contribution], {}).for_plugin("security")

    assert config.secret("BLITZE_TOKEN").get_secret_value() == ""
    assert not config.is_set("BLITZE_TOKEN")


def test_a_plugin_that_declares_nothing_gets_an_empty_configuration(tmp_path):
    roles = tmp_path / "roles"
    roles.mkdir()
    contribution = AnsibleContribution("resolver", roles)

    resolved = resolve_capability_environment([contribution], {})

    assert resolved.for_plugin("resolver").values == {}


def test_a_required_key_that_is_unset_stops_the_control_plane(tmp_path):
    """Named by capability and by key, at composition, before anything runs."""
    roles = tmp_path / "roles"
    roles.mkdir()
    contribution = AnsibleContribution(
        "widget",
        roles,
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
        resolve_capability_environment([contribution], {})


def test_a_value_shorter_than_the_capability_declared_is_refused(tmp_path):
    """The placeholder case, refused where it is cheap.

    A signing secret somebody meant to replace used to start a controller, be
    forwarded to every play, and be reported only by the first site that turned
    the feature on. The length is a rule core can hold without knowing what the
    value means, so it holds it.
    """
    roles = tmp_path / "roles"
    roles.mkdir()
    contribution = AnsibleContribution(
        "security",
        roles,
        environment_keys=(EnvironmentKey(name="BLITZE_TOKEN", minimum_bytes=32),),
    )

    with pytest.raises(ConfigurationError, match=r"BLITZE_TOKEN.*32 bytes.*security"):
        resolve_capability_environment([contribution], {"BLITZE_TOKEN": SecretStr("x")})

    resolved = resolve_capability_environment(
        [contribution], {"BLITZE_TOKEN": SecretStr("x" * 32)}
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
    contribution = AnsibleContribution(
        "security",
        roles,
        environment_keys=(EnvironmentKey(name="BLITZE_TOKEN", minimum_bytes=32),),
    )

    resolved = resolve_capability_environment([contribution], {})

    assert not resolved.for_plugin("security").is_set("BLITZE_TOKEN")


def test_duplicate_environment_ownership_names_both_plugins(tmp_path):
    roles = tmp_path / "roles"
    roles.mkdir()
    contributions = [
        AnsibleContribution(
            "alpha", roles, environment_keys=(EnvironmentKey(name="BLITZE_TOKEN"),)
        ),
        AnsibleContribution(
            "zulu", roles, environment_keys=(EnvironmentKey(name="BLITZE_TOKEN"),)
        ),
    ]

    with pytest.raises(PluginError, match=r"BLITZE_TOKEN.*alpha.*zulu"):
        resolve_capability_environment(contributions, {})


def test_unknown_capability_environment_is_a_configuration_error():
    with pytest.raises(PluginError, match=r"unknown.*BLITZE_TYPO"):
        resolve_capability_environment([], {"BLITZE_TYPO": SecretStr("value")})
