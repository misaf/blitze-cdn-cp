from pathlib import Path

import pytest

from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import ConfigurationError


def test_environment_configuration_and_precedence(tmp_path: Path):
    env = {
        "BLITZE_API_KEY": "a" * 32,
        "BLITZE_API_KEYS": f"alice:{'b' * 32}",
        "BLITZE_DEPLOYMENT_TIMEOUT_SECONDS": "120",
        "BLITZE_ALLOW_EMPTY_SITES": "true",
    }
    settings = Settings.from_environment(env, project_dir=tmp_path)
    assert set(settings.api_keys) == {"alice", "default"}
    assert settings.deployment_timeout_seconds == 120
    assert settings.state_dir == (tmp_path / ".state").resolve()
    assert settings.allow_empty_sites is True


@pytest.mark.parametrize(
    "environment",
    [
        {"BLITZE_API_KEY": "short"},
        {"BLITZE_API_KEYS": "missing-separator"},
        {"BLITZE_DEPLOYMENT_TIMEOUT_SECONDS": "not-a-number"},
        {"BLITZE_ALLOW_EMPTY_SITES": "sometimes"},
        {"BLITZE_UNDER_ATTACK_SECRET": "too-short"},
    ],
)
def test_invalid_environment_fails_closed(tmp_path, environment):
    with pytest.raises(ConfigurationError):
        Settings.from_environment(environment, project_dir=tmp_path)


def test_runtime_validation_reports_missing_files(tmp_path):
    settings = Settings.from_environment({}, project_dir=tmp_path)
    errors = settings.validate_runtime(require_auth=True)
    assert "no API keys configured" in errors
    assert any("inventory does not exist" in item for item in errors)


def test_under_attack_secret_is_environment_only_and_masked(tmp_path):
    settings = Settings.from_environment(
        {"BLITZE_UNDER_ATTACK_SECRET": "s" * 32}, project_dir=tmp_path
    )

    assert settings.under_attack_secret.get_secret_value() == "s" * 32
    assert "s" * 32 not in repr(settings)


def test_runtime_validation_requires_generated_vars_beneath_state(settings):
    nested_in_ansible = settings.model_copy(
        update={"generated_vars_path": settings.ansible_dir / "generated/state.yml"}
    )
    assert any(
        "generated vars must be a file under the state directory" in error
        for error in nested_in_ansible.validate_runtime()
    )

    nested_in_state = settings.model_copy(
        update={"generated_vars_path": settings.state_dir / "generated/state.yml"}
    )
    assert not any(
        "generated vars" in error for error in nested_in_state.validate_runtime()
    )


def test_preflight_dns_servers_default_to_the_host_resolver(tmp_path):
    assert (
        Settings.from_environment({}, project_dir=tmp_path).preflight_dns_servers == ()
    )


@pytest.mark.parametrize("raw", ["dns.google", "not-an-ip", "1.1.1.1,nope"])
def test_preflight_dns_servers_must_be_addresses(tmp_path, raw):
    """A hostname would have to be resolved by the resolver we do not trust."""
    with pytest.raises(ConfigurationError, match="not an IP address"):
        Settings.from_environment(
            {"BLITZE_PREFLIGHT_DNS_SERVERS": raw}, project_dir=tmp_path
        )
