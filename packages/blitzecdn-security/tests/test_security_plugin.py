from dataclasses import dataclass
from pathlib import Path

import pytest
from blitzecdn_security import plugin
from blitzecdn_security.config import MINIMUM_SECRET_BYTES, SECRET_VARIABLE
from pydantic import SecretStr

from blitzecdn.capabilities.sites.domain import CdnSite
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.plugins import (
    ResolvedCapabilityEnvironment,
    resolve_capability_environment,
)


def _site(*, under_attack: bool, enabled: bool = True) -> CdnSite:
    return CdnSite(
        name="alpha",
        server_names=("alpha.example.com",),
        origin_host="198.51.100.10",
        compression="off",
        enabled=enabled,
        under_attack_mode=under_attack,
    )


@dataclass(frozen=True)
class _Platform:
    """The one attribute this package reads off the control plane.

    `capability_config` is resolved from this package's *real* contribution
    below, so the declaration the plugin ships and the configuration these
    tests hand it cannot drift: a key the contribution stopped claiming would
    fail here rather than quietly read as unset.
    """

    capability_config: ResolvedCapabilityEnvironment


def _platform(secret: str) -> _Platform:
    configured = {SECRET_VARIABLE: SecretStr(secret)} if secret else {}
    return _Platform(
        resolve_capability_environment(
            plugin.blitzecdn_capability_configuration(),
            configured,
            # No `blitzecdn.toml`: this capability's only configuration is a
            # secret, and a secret is never satisfied from the committed file.
            {},
            Path("/nonexistent"),
        )
    )


def test_metadata_provides_optional_security_capability() -> None:
    metadata = plugin.blitzecdn_plugin_metadata()
    assert metadata.name == "security"
    assert metadata.capabilities == frozenset({"security"})
    assert not metadata.required


def test_under_attack_without_a_controller_secret_is_a_blocking_issue() -> None:
    platform = _platform("")

    (issue,) = plugin.blitzecdn_deployment_checks(_site(under_attack=True), platform)

    assert issue.plugin == "security"
    assert issue.site == "alpha"
    assert issue.severity == "blocking"
    assert "BLITZE_UNDER_ATTACK_SECRET" in issue.message


def test_no_issue_when_security_is_unused_or_implementable() -> None:
    provisioned = _platform("x" * 32)
    missing = _platform("")
    checks = plugin.blitzecdn_deployment_checks

    assert checks(_site(under_attack=True), provisioned) == ()
    assert checks(_site(under_attack=False), missing) == ()
    assert checks(_site(under_attack=True, enabled=False), missing) == ()


def test_a_secret_too_short_to_sign_with_never_reaches_this_capability() -> None:
    """A placeholder is the usual way this goes wrong, and it is not a secret.

    This used to be a deployment check beside the one above: the controller
    started with the placeholder, forwarded it to every play, and reported it
    only for a site that turned Under Attack Mode on. The length is now
    declared on the contribution — `minimum_bytes` — so core refuses the
    controller's configuration at composition, before a service exists, naming
    the key and this capability. The role still refuses to converge with a
    short key; what changed is that nothing gets that far.
    """
    with pytest.raises(
        ConfigurationError, match=rf"{SECRET_VARIABLE}.*{MINIMUM_SECRET_BYTES} bytes"
    ):
        _platform("short")


def test_the_edge_role_travels_with_the_distribution() -> None:
    """Installing the wheel is what puts the challenge implementation on edges.

    The contribution is the whole of how core learns the role exists and that
    the edge play should run it; nothing in the control plane names either.
    """
    (contribution,) = plugin.blitzecdn_ansible_contributions()

    assert contribution.plugin == "security"
    assert contribution.roles_path.is_dir()
    assert contribution.edge_roles == ("blitzecdn_security",)
    assert (contribution.roles_path / "blitzecdn_security").is_dir()
