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


def test_a_capabilitys_settings_are_kept_environment_only_and_masked(tmp_path):
    """Core holds an optional capability's configuration without knowing it.

    `BLITZE_UNDER_ATTACK_SECRET` used to be a field on this model, which meant
    every installation loaded a setting named for a distribution most of them
    do not have — and a capability nobody here had heard of could not be
    configured at all. Now core keeps whatever `BLITZE_*` it was given and does
    not consume, and the owning package reads it.

    Masked, because core cannot tell a credential from a tuning value, and the
    safe assumption for a value it cannot interpret is that it is one.
    """
    settings = Settings.from_environment(
        {
            "BLITZE_UNDER_ATTACK_SECRET": "s" * 32,
            "BLITZE_SOMETHING_UNHEARD_OF": "42",
        },
        project_dir=tmp_path,
    )

    kept = settings.capability_environment
    assert kept["BLITZE_UNDER_ATTACK_SECRET"].get_secret_value() == "s" * 32
    assert kept["BLITZE_SOMETHING_UNHEARD_OF"].get_secret_value() == "42"
    assert "s" * 32 not in repr(settings)


def test_a_capabilitys_settings_may_not_come_from_the_committed_toml(tmp_path):
    """`.env` is 0600 and uncommitted; blitzecdn.toml is neither.

    The TOML reader refuses a key it does not know, and no capability name is
    among the ones it knows, so a credential cannot arrive that way whatever a
    package documents.
    """
    (tmp_path / "blitzecdn.toml").write_text(
        '[blitzecdn]\nunder_attack_secret = "s"\n', encoding="utf-8"
    )

    with pytest.raises(ConfigurationError, match="unknown project configuration"):
        Settings.from_environment({}, project_dir=tmp_path)


def test_the_controllers_own_names_are_not_handed_to_a_capability(tmp_path):
    """Core's settings stay core's, most importantly its API keys."""
    settings = Settings.from_environment(
        {
            "BLITZE_API_KEY": "k" * 40,
            "BLITZE_RUN_LOG_RETENTION": "42",
            "BLITZE_REDIS_URL": "redis://127.0.0.1:6379/1",
        },
        project_dir=tmp_path,
    )

    assert settings.capability_environment == {}
    assert settings.run_log_retention == 42


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
