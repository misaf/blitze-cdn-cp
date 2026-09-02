from dataclasses import dataclass

from blitzecdn_security import plugin
from blitzecdn_security.config import SECRET_VARIABLE
from pydantic import SecretStr

from blitzecdn.features.sites.domain import CdnSite


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
class _Settings:
    """The one attribute this package reads off core's settings.

    `capability_environment` stages non-core `BLITZE_*` values. This package's
    Ansible contribution explicitly claims its key before composition permits
    it to reach Ansible. Core carries no named field for the optional secret.
    """

    capability_environment: dict[str, SecretStr]


@dataclass(frozen=True)
class _Platform:
    settings: _Settings


def _platform(secret: str) -> _Platform:
    return _Platform(_Settings({SECRET_VARIABLE: SecretStr(secret)} if secret else {}))


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


def test_a_secret_too_short_to_sign_with_is_refused_like_a_missing_one() -> None:
    """A placeholder is the usual way this goes wrong, and it is not a secret.

    The role refuses to converge with a short key too. Catching it here is what
    turns a rolled-back deploy on every edge into a message at
    `blitzecdn validate` naming the variable.
    """
    (issue,) = plugin.blitzecdn_deployment_checks(
        _site(under_attack=True), _platform("short")
    )

    assert SECRET_VARIABLE in issue.message


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
