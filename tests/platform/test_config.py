import shutil
from pathlib import Path

import pytest
from support.paths import CORE_ANSIBLE, REPO_ROOT

from blitzecdn.composition import load_registry
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.features.resolution import resolve_capability_environment


def test_unconfigured_values_use_model_defaults(tmp_path, monkeypatch):
    class CustomSettings(Settings):
        deployment_timeout_seconds: int = 240
        allow_empty_sites: bool = True

    monkeypatch.setenv("DEPLOYMENT_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("ALLOW_EMPTY_SITES", "false")
    monkeypatch.setenv("BLITZE_DEPLOYMENT_TIMEOUT_SECONDS", "360")
    settings = CustomSettings.from_environment({}, project_dir=tmp_path)

    assert settings.deployment_timeout_seconds == 240
    assert settings.allow_empty_sites is True


def test_configured_values_preserve_precedence_and_falsey_values(tmp_path):
    (tmp_path / "blitzecdn.toml").write_text(
        "[blitzecdn]\ndeployment_timeout_seconds = 120\n"
        "allow_empty_sites = false\ndrift_check_interval_seconds = 0\n"
    )
    configured = Settings.from_environment({}, project_dir=tmp_path)
    assert configured.deployment_timeout_seconds == 120
    assert configured.allow_empty_sites is False
    assert configured.drift_check_interval_seconds == 0

    (tmp_path / ".env").write_text("BLITZE_DEPLOYMENT_TIMEOUT_SECONDS=180\n")
    assert (
        Settings.from_environment({}, project_dir=tmp_path).deployment_timeout_seconds
        == 180
    )
    overridden = Settings.from_environment(
        {"BLITZE_DEPLOYMENT_TIMEOUT_SECONDS": "240"}, project_dir=tmp_path
    )
    assert overridden.deployment_timeout_seconds == 240


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


def test_the_platform_ansible_is_not_a_function_of_the_project_directory(tmp_path):
    """An empty project directory still has roles, plays and an inventory.

    They come from `importlib.resources`, so a controller whose project directory
    is not a checkout has them too; only the API key is missing.
    """
    settings = Settings.from_environment({}, project_dir=tmp_path)

    assert settings.project_dir == tmp_path.resolve()
    assert settings.ansible_dir == CORE_ANSIBLE
    assert settings.playbook_path == CORE_ANSIBLE / "playbooks/edge.yml"
    assert settings.inventory_path == CORE_ANSIBLE / "inventory/blitzecdn.yml"
    assert not any("does not exist" in item for item in settings.validate_runtime())


def test_a_capabilitys_settings_are_kept_environment_only_and_masked(tmp_path):
    """Core holds a capability's configuration without knowing it.

    Core keeps whatever `BLITZE_*` it was given and does not consume, and the
    owning capability reads it. Masked, because core cannot tell a credential from
    a tuning value.
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


def test_a_capabilitys_settings_may_come_from_the_committed_toml(tmp_path):
    """A capability's non-secret settings belong in the non-secret file.

    An unrecognised TOML key is staged under its `BLITZE_` name and claimed later
    by a feature. Secrets stay out: file keys are staged separately, only a
    `CapabilitySetting` is satisfied from them, and a declared `EnvironmentKey`
    appearing there is refused.
    """
    (tmp_path / "blitzecdn.toml").write_text(
        "[blitzecdn]\ncertificate_renewal_interval_seconds = 3600\n", encoding="utf-8"
    )

    settings = Settings.from_environment({}, project_dir=tmp_path)

    assert settings.capability_config_file == {
        "BLITZE_CERTIFICATE_RENEWAL_INTERVAL_SECONDS": "3600"
    }
    # And not into the map a secret is read from.
    assert settings.capability_environment == {}


def test_the_controllers_own_names_are_not_handed_to_a_capability(tmp_path):
    """Core's settings stay core's, most importantly its API keys."""
    settings = Settings.from_environment(
        {
            "BLITZE_API_KEY": "k" * 40,
            "BLITZE_RUN_LOG_RETENTION": "42",
            "BLITZE_WORKER_POLL_SECONDS": "2.5",
        },
        project_dir=tmp_path,
    )

    assert settings.capability_environment == {}
    assert settings.run_log_retention == 42
    assert settings.worker_poll_seconds == 2.5


def test_the_shipped_example_configuration_is_accepted_as_written(tmp_path):
    """`blitzecdn.example.toml`, copied unedited, must start a control plane.

    An unclaimed key in it would be a refusal every new installation meets first.
    Resolved against the shipped features, which declare every setting.
    """
    features = load_registry()
    shutil.copy(REPO_ROOT / "blitzecdn.example.toml", tmp_path / "blitzecdn.toml")

    settings = Settings.from_environment({}, project_dir=tmp_path)

    resolve_capability_environment(
        features.configuration_contributions(),
        settings.capability_environment,
        settings.capability_config_file,
        settings.state_dir,
    )


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


def test_allowed_ips_sources_and_normalization(tmp_path):
    (tmp_path / "blitzecdn.toml").write_text(
        '[blitzecdn]\nallowed_ips = ["203.0.113.8", "198.51.100.0/24"]\n'
    )
    configured = Settings.from_environment({}, project_dir=tmp_path)
    assert configured.allowed_ips == ("203.0.113.8/32", "198.51.100.0/24")
    overridden = Settings.from_environment(
        {"BLITZE_ALLOWED_IPS": "198.51.100.8,198.51.100.8/32"}, project_dir=tmp_path
    )
    assert overridden.allowed_ips == ("198.51.100.8/32",)
    assert not overridden.capability_environment
    assert not overridden.capability_config_file
    assert (
        Settings.from_environment(
            {"BLITZE_ALLOWED_IPS": ""}, project_dir=tmp_path
        ).allowed_ips
        == ()
    )


@pytest.mark.parametrize(
    "value",
    [
        "example.com",
        "203.0.113.8/99",
        "203.0.113.8,",
        "*",
        # IPv6 parses as a network and would still admit nobody: the installed
        # listener is IPv4.
        "2001:db8::1",
        "2001:db8::/64",
        "::ffff:198.51.100.8/128",
        "203.0.113.8,2001:db8::1",
        # Host bits set. Masking them away would admit the whole range.
        "203.0.113.8/24",
        "10.1.2.3/8",
    ],
)
def test_invalid_allowed_ips_fail_configuration(tmp_path, value):
    with pytest.raises(ConfigurationError):
        Settings.from_environment({"BLITZE_ALLOWED_IPS": value}, project_dir=tmp_path)


def test_an_entry_with_host_bits_offers_both_readings(tmp_path):
    """The one error where guessing would admit people nobody named.

    `203.0.113.8/24` is one host or 256; neither is assumed, and both spellings
    are offered.
    """
    with pytest.raises(ConfigurationError) as failure:
        Settings.from_environment(
            {"BLITZE_ALLOWED_IPS": "203.0.113.8/24"}, project_dir=tmp_path
        )

    assert "203.0.113.0/24" in str(failure.value)
    assert "203.0.113.8/32" in str(failure.value)
