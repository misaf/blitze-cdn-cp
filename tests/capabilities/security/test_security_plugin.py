from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import SecretStr

from blitzecdn.capabilities.security.config import (
    MINIMUM_SECRET_BYTES,
    SECRET_VARIABLE,
    SecurityConfig,
)
from blitzecdn.capabilities.security.feature import FEATURES
from blitzecdn.capabilities.security.runtime import security_rendering
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.capabilities.serving.policy import SitePolicy
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.plugins import ValidationIssue, resolve_capability_environment

(FEATURE,) = FEATURES


def _site(*, under_attack: bool, enabled: bool = True) -> CdnSite:
    return CdnSite(
        name="alpha",
        server_names=("alpha.example.com",),
        origin_host="198.51.100.10",
        policy=SitePolicy.from_settings(
            {
                "compression": "off",
                "enabled": enabled,
                "under_attack_mode": under_attack,
            }
        ),
    )


def _checks(secret: str):
    """This feature's deployment checks, configured from its real declaration.

    The configuration is resolved from the contribution the feature ships, so
    the declaration and the configuration these tests hand it cannot drift: a
    key the contribution stopped claiming would fail here rather than quietly
    read as unset.
    """
    configured = {SECRET_VARIABLE: SecretStr(secret)} if secret else {}
    resolved = resolve_capability_environment(
        FEATURE.configuration,
        configured,
        # No `blitzecdn.toml`: this capability's only configuration is a
        # secret, and a secret is never satisfied from the committed file.
        {},
        Path("/nonexistent"),
    )
    rendering = security_rendering(
        config=SecurityConfig.from_capability_config(resolved.for_plugin("security"))
    )
    assert rendering.deployment_checks is not None
    checks = rendering.deployment_checks

    def run(site: CdnSite) -> Sequence[ValidationIssue]:
        return tuple(checks(site))

    return run


def test_metadata_provides_required_security_capability() -> None:
    metadata = FEATURE.metadata
    assert metadata.name == "security"
    assert metadata.capabilities == frozenset({"security"})


def test_under_attack_without_a_controller_secret_is_a_blocking_issue() -> None:
    (issue,) = _checks("")(_site(under_attack=True))

    assert issue.plugin == "security"
    assert issue.site == "alpha"
    assert issue.severity == "blocking"
    assert "BLITZE_UNDER_ATTACK_SECRET" in issue.message


def test_no_issue_when_security_is_unused_or_implementable() -> None:
    provisioned = _checks("x" * 32)
    missing = _checks("")

    assert provisioned(_site(under_attack=True)) == ()
    assert missing(_site(under_attack=False)) == ()
    assert missing(_site(under_attack=True, enabled=False)) == ()


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
        _checks("short")


def test_the_edge_role_travels_with_the_distribution() -> None:
    """Installing the wheel is what puts the challenge implementation on edges.

    The contribution is the whole of how core learns the role exists and that
    the edge play should run it; nothing in the control plane names either.
    """
    (contribution,) = FEATURE.ansible

    assert contribution.plugin == "security"
    assert contribution.roles_path.is_dir()
    assert contribution.edge_roles == ("blitzecdn_security",)
    assert (contribution.roles_path / "blitzecdn_security").is_dir()
