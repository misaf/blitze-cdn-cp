"""The static resource seam optional Nginx implementations cross."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from blitzecdn.core.exceptions import PluginError
from blitzecdn.core.nginx import (
    resolve_capability_environment,
    resolve_nginx_resources,
)
from blitzecdn.core.plugins import AnsibleContribution, NginxContribution, load_plugins


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
        environment_keys=("BLITZE_MAXMIND_LICENSE_KEY",),
    )
    configured = {"BLITZE_MAXMIND_LICENSE_KEY": SecretStr("sentinel")}

    assert resolve_capability_environment([contribution], configured) == configured


def test_duplicate_environment_ownership_names_both_plugins(tmp_path):
    roles = tmp_path / "roles"
    roles.mkdir()
    contributions = [
        AnsibleContribution("alpha", roles, environment_keys=("BLITZE_TOKEN",)),
        AnsibleContribution("zulu", roles, environment_keys=("BLITZE_TOKEN",)),
    ]

    with pytest.raises(PluginError, match=r"BLITZE_TOKEN.*alpha.*zulu"):
        resolve_capability_environment(contributions, {})


def test_unknown_capability_environment_is_a_configuration_error():
    with pytest.raises(PluginError, match=r"unknown.*BLITZE_TYPO"):
        resolve_capability_environment([], {"BLITZE_TYPO": SecretStr("value")})
