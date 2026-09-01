from dataclasses import dataclass

from blitzecdn_security import plugin
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
    under_attack_secret: SecretStr


@dataclass(frozen=True)
class _Platform:
    settings: _Settings


def test_metadata_provides_optional_security_capability() -> None:
    metadata = plugin.blitzecdn_plugin_metadata()
    assert metadata.name == "security"
    assert metadata.capabilities == frozenset({"security"})
    assert not metadata.required


def test_under_attack_without_a_controller_secret_is_a_blocking_issue() -> None:
    platform = _Platform(_Settings(SecretStr("")))

    (issue,) = plugin.blitzecdn_deployment_checks(_site(under_attack=True), platform)

    assert issue.plugin == "security"
    assert issue.site == "alpha"
    assert issue.severity == "blocking"
    assert "BLITZE_UNDER_ATTACK_SECRET" in issue.message


def test_no_issue_when_security_is_unused_or_implementable() -> None:
    provisioned = _Platform(_Settings(SecretStr("x" * 32)))
    missing = _Platform(_Settings(SecretStr("")))
    checks = plugin.blitzecdn_deployment_checks

    assert checks(_site(under_attack=True), provisioned) == ()
    assert checks(_site(under_attack=False), missing) == ()
    assert checks(_site(under_attack=True, enabled=False), missing) == ()
