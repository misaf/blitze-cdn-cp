from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import SecretStr

from blitzecdn.capabilities.security.feature import FEATURES
from blitzecdn.capabilities.security.settings import (
    MINIMUM_SECRET_BYTES,
    SECRET_VARIABLE,
    SecurityConfig,
)
from blitzecdn.capabilities.security.wiring import security_rendering
from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.capabilities.serving.domain import CdnSite
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.features import ValidationIssue, resolve_capability_environment

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

    A key the contribution stopped claiming fails here rather than reading as
    unset.
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
        config=resolved.for_feature("security").as_model(SecurityConfig)
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

    assert issue.feature == "security"
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

    The contribution declares `minimum_bytes`, so core refuses the configuration
    at composition, naming the key and this capability. The role still refuses a
    short key as well.
    """
    with pytest.raises(
        ConfigurationError, match=rf"{SECRET_VARIABLE}.*{MINIMUM_SECRET_BYTES} bytes"
    ):
        _checks("short")


def test_the_edge_role_travels_with_the_capability() -> None:
    """The contribution is how core learns the challenge role exists and that the
    edge play should run it; nothing in the control plane names either.
    """
    (contribution,) = FEATURE.ansible

    assert contribution.feature == "security"
    assert contribution.roles_path.is_dir()
    assert contribution.edge_roles == ("blitzecdn_security",)
    assert (contribution.roles_path / "blitzecdn_security").is_dir()
