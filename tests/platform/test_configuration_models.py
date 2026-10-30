"""Configuration behavior at its real loading and composition boundaries."""

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import Field, SecretStr

from blitzecdn.capabilities.backup.settings import BackupConfig
from blitzecdn.capabilities.security.geoip.settings import GeoIpConfig
from blitzecdn.capabilities.security.settings import SecurityConfig
from blitzecdn.capabilities.tls.settings import CertificateConfig
from blitzecdn.composition import load_registry
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import ConfigurationError, FeatureError
from blitzecdn.core.features import (
    CapabilitySetting,
    ConfigurationModel,
    resolve_capability_environment,
    resolve_feature_configuration,
)


def resolve(settings):
    return resolve_capability_environment(
        load_registry().configuration_contributions(),
        settings.capability_environment,
        settings.capability_config_file,
        settings.state_dir,
    )


def test_typed_certificate_settings_preserve_sources_and_normalization(tmp_path):
    (tmp_path / "blitzecdn.toml").write_text(
        "[blitzecdn]\ncertificate_renewal_interval_seconds = 7200\n"
        'acme_default_email = " Admin@Example.COM "\n'
    )
    (tmp_path / ".env").write_text("BLITZE_CERTIFICATE_RENEWAL_INTERVAL_SECONDS=3600\n")
    settings = Settings.from_environment(
        {
            "BLITZE_CERTIFICATE_RENEWAL_INTERVAL_SECONDS": "0",
            "BLITZE_ACME_CA_DOMAIN": " LetsEncrypt.ORG. ",
        },
        project_dir=tmp_path,
    )

    scoped = resolve(settings).for_feature("certificates")
    config = scoped.as_model(CertificateConfig)

    assert config.renewal_interval_seconds == 0
    assert config.default_email == "admin@example.com"
    assert config.ca_domain == "letsencrypt.org"
    assert scoped.as_model(CertificateConfig) is config


@pytest.mark.parametrize(
    "environment",
    [
        {"BLITZE_CERTIFICATE_RENEWAL_BUDGET_SECONDS": "29"},
        {"BLITZE_CERTIFICATE_RENEWAL_BUDGET_SECONDS": "7201"},
        {"BLITZE_ACME_CA_DOMAIN": "not a CA"},
    ],
)
def test_invalid_typed_settings_fail_during_resolution(tmp_path, environment):
    settings = Settings.from_environment(environment, project_dir=tmp_path)
    with pytest.raises(ConfigurationError, match="certificates"):
        resolve(settings)


def test_blank_environment_keeps_default_instead_of_lower_priority_file(tmp_path):
    (tmp_path / "blitzecdn.toml").write_text(
        "[blitzecdn]\ncertificate_renewal_interval_seconds = 7200\n"
    )
    settings = Settings.from_environment(
        {"BLITZE_CERTIFICATE_RENEWAL_INTERVAL_SECONDS": " "},
        project_dir=tmp_path,
    )
    config = resolve(settings).for_feature("certificates").as_model(CertificateConfig)
    assert config.renewal_interval_seconds == 43_200


def test_only_declared_secrets_reach_ansible(tmp_path):
    secret = "private-signing-key-" * 3
    settings = Settings.from_environment(
        {
            "BLITZE_API_KEY": "operator-api-key-" * 3,
            "BLITZE_UNDER_ATTACK_SECRET": secret,
            "BLITZE_CERTBOT": "/opt/bin/certbot",
        },
        project_dir=tmp_path,
    )
    configured = resolve(settings)
    config = configured.for_feature("security").as_model(SecurityConfig)
    assert config.challenge_available
    assert config.under_attack_secret.get_secret_value() == secret
    assert secret not in repr(config)
    assert set(configured.environment) == {"BLITZE_UNDER_ATTACK_SECRET"}


def test_typed_secret_cannot_be_loaded_from_toml(tmp_path):
    (tmp_path / "blitzecdn.toml").write_text(
        '[blitzecdn]\nunder_attack_secret = "' + "secret" * 8 + '"\n'
    )
    with pytest.raises(ConfigurationError, match=r"secrets.*under_attack_secret"):
        resolve(Settings.from_environment({}, project_dir=tmp_path))


def test_backup_settings_resolve_without_a_database(tmp_path):
    settings = Settings.from_environment(
        {"BLITZE_BACKUP_DIR": "archives"}, project_dir=tmp_path
    )
    config = BackupConfig.from_settings(settings)
    assert config.backup_dir == settings.state_dir / "archives"
    assert not settings.database_path.exists()


def test_a_new_typed_setting_needs_only_its_model_field(tmp_path):
    class SettingsForTest(ConfigurationModel):
        enabled: bool = Field(default=True, alias="BLITZE_EXAMPLE_ENABLED")
        retries: int = Field(default=3, ge=0, le=5, alias="BLITZE_EXAMPLE_RETRIES")
        directory: Path = Field(default=Path("work"), alias="BLITZE_EXAMPLE_DIR")

    declarations = [SettingsForTest.configuration("example")]
    resolved = resolve_capability_environment(
        declarations,
        {"BLITZE_EXAMPLE_ENABLED": SecretStr("false")},
        {"BLITZE_EXAMPLE_RETRIES": "0"},
        tmp_path,
    )
    config = resolved.for_feature("example").as_model(SettingsForTest)
    assert config.enabled is False
    assert config.retries == 0
    assert config.directory == tmp_path / "work"
    assert resolved.environment == {}
    with pytest.raises(ConfigurationError, match="BLITZE_EXAMPLE_RETRIES"):
        resolve_capability_environment(
            declarations, {}, {"BLITZE_EXAMPLE_RETRIES": "6"}, tmp_path
        )


class ExampleConfig(ConfigurationModel):
    count: int = Field(default=600, ge=0, le=86_400, alias="BLITZE_COUNT")
    enabled: bool = Field(default=True, alias="BLITZE_ENABLED")
    name: str = Field(default="certbot", alias="BLITZE_NAME")
    directory: Path = Field(default=Path("backups"), alias="BLITZE_DIRECTORY")


def test_features_cannot_read_each_others_secrets(tmp_path):
    configured = {"BLITZE_MAXMIND_LICENSE_KEY": SecretStr("sentinel")}
    resolved = resolve_capability_environment(
        [GeoIpConfig.configuration("geoip"), SecurityConfig.configuration("security")],
        configured,
        {},
        tmp_path,
    )
    geoip = resolved.for_feature("geoip")
    assert resolved.environment == configured
    assert geoip.secret("BLITZE_MAXMIND_LICENSE_KEY").get_secret_value() == "sentinel"
    assert geoip.is_set("BLITZE_MAXMIND_LICENSE_KEY")
    with pytest.raises(
        FeatureError, match=r"geoip.*BLITZE_UNDER_ATTACK_SECRET.*does not declare"
    ):
        geoip.secret("BLITZE_UNDER_ATTACK_SECRET")


def test_unset_secrets_remain_declared_and_empty(tmp_path):
    scoped = resolve_capability_environment(
        [SecurityConfig.configuration("security")],
        {},
        {},
        tmp_path,
    ).for_feature("security")
    assert scoped.secret("BLITZE_UNDER_ATTACK_SECRET").get_secret_value() == ""
    assert not scoped.is_set("BLITZE_UNDER_ATTACK_SECRET")


def test_a_feature_without_configuration_gets_an_empty_scope(tmp_path):
    scoped = resolve_capability_environment([], {}, {}, tmp_path).for_feature(
        "resolver"
    )
    assert scoped.values == {}
    assert scoped.settings == {}
    with pytest.raises(FeatureError, match=r"resolver.*ExampleConfig"):
        scoped.as_model(ExampleConfig)


def test_a_resolved_configuration_cannot_be_read_as_another_model(tmp_path):
    scoped = resolve_capability_environment(
        [ExampleConfig.configuration("example")],
        {},
        {},
        tmp_path,
    ).for_feature("example")
    with pytest.raises(FeatureError, match=r"example.*CertificateConfig"):
        scoped.as_model(CertificateConfig)


def test_a_required_secret_fails_before_any_service_runs(tmp_path):
    contribution = GeoIpConfig.configuration("geoip")
    contribution = replace(
        contribution,
        environment_keys=(
            replace(contribution.environment_keys[0], required=True),
            contribution.environment_keys[1],
        ),
    )
    with pytest.raises(ConfigurationError, match=r"geoip.*BLITZE_MAXMIND_ACCOUNT_ID"):
        resolve_capability_environment([contribution], {}, {}, tmp_path)


def test_secret_minimum_length_is_independent_of_presence(tmp_path):
    contribution = SecurityConfig.configuration("security")
    resolved = resolve_capability_environment([contribution], {}, {}, tmp_path)
    assert not resolved.for_feature("security").is_set("BLITZE_UNDER_ATTACK_SECRET")
    with pytest.raises(
        ConfigurationError, match=r"BLITZE_UNDER_ATTACK_SECRET.*32 bytes.*security"
    ):
        resolve_capability_environment(
            [contribution],
            {"BLITZE_UNDER_ATTACK_SECRET": SecretStr("x")},
            {},
            tmp_path,
        )
    resolved = resolve_capability_environment(
        [contribution],
        {"BLITZE_UNDER_ATTACK_SECRET": SecretStr("x" * 32)},
        {},
        tmp_path,
    )
    assert resolved.for_feature("security").is_set("BLITZE_UNDER_ATTACK_SECRET")


def test_duplicate_configuration_ownership_names_both_features(tmp_path):
    with pytest.raises(FeatureError, match=r"BLITZE_COUNT.*alpha.*zulu"):
        resolve_capability_environment(
            [ExampleConfig.configuration("alpha"), ExampleConfig.configuration("zulu")],
            {},
            {},
            tmp_path,
        )


def test_a_name_cannot_be_both_a_secret_and_a_setting(tmp_path):
    class SecretConfig(ConfigurationModel):
        count: SecretStr = Field(default=SecretStr(""), alias="BLITZE_COUNT")

    with pytest.raises(FeatureError, match=r"BLITZE_COUNT.*alpha.*zulu"):
        resolve_capability_environment(
            [SecretConfig.configuration("alpha"), ExampleConfig.configuration("zulu")],
            {},
            {},
            tmp_path,
        )


def test_unknown_capability_configuration_names_the_typo(tmp_path):
    with pytest.raises(FeatureError, match=r"unknown.*BLITZE_TYPO"):
        resolve_capability_environment(
            [], {"BLITZE_TYPO": SecretStr("value")}, {}, tmp_path
        )


def test_unconfigured_settings_keep_typed_defaults(tmp_path):
    scoped = resolve_capability_environment(
        [ExampleConfig.configuration("example")],
        {},
        {},
        tmp_path,
    ).for_feature("example")
    config = scoped.as_model(ExampleConfig)
    assert config.count == 600
    assert config.enabled is True
    assert config.name == "certbot"
    assert config.directory == tmp_path / "backups"


def test_configured_settings_are_typed_and_not_forwarded_to_ansible(tmp_path):
    resolved = resolve_capability_environment(
        [ExampleConfig.configuration("example")],
        {
            "BLITZE_COUNT": SecretStr("7"),
            "BLITZE_ENABLED": SecretStr("false"),
            "BLITZE_NAME": SecretStr("custom-certbot"),
            "BLITZE_DIRECTORY": SecretStr("/var/backups/blitzecdn"),
        },
        {},
        tmp_path,
    )
    config = resolved.for_feature("example").as_model(ExampleConfig)
    assert config.count == 7
    assert config.enabled is False
    assert config.name == "custom-certbot"
    assert config.directory == Path("/var/backups/blitzecdn")
    assert resolved.environment == {}


@pytest.mark.parametrize("value", ["perhaps", "falsehood"])
def test_a_word_is_never_read_as_a_flag(tmp_path, value):
    with pytest.raises(ConfigurationError, match=r"example.*(?s:.*)BLITZE_ENABLED"):
        resolve_capability_environment(
            [ExampleConfig.configuration("example")],
            {"BLITZE_ENABLED": SecretStr(value)},
            {},
            tmp_path,
        )


@pytest.mark.parametrize("value", ["-1", "86401", "not-a-number"])
def test_invalid_numbers_fail_during_resolution(tmp_path, value):
    with pytest.raises(ConfigurationError, match=r"example.*(?s:.*)BLITZE_COUNT"):
        resolve_capability_environment(
            [ExampleConfig.configuration("example")],
            {"BLITZE_COUNT": SecretStr(value)},
            {},
            tmp_path,
        )


def test_direct_resolution_rejects_secrets_in_toml(tmp_path):
    with pytest.raises(ConfigurationError, match=r"secrets.*under_attack_secret"):
        resolve_feature_configuration(
            SecurityConfig.configuration("security"),
            {},
            {"BLITZE_UNDER_ATTACK_SECRET": "s" * 32},
            tmp_path,
        )


def test_a_setting_may_not_declare_bounds_on_something_uncountable():
    with pytest.raises(ValueError, match=r"BLITZE_NAME.*whole numbers only"):
        CapabilitySetting(name="BLITZE_NAME", default="certbot", minimum=1)


def test_a_default_outside_its_own_bounds_is_refused():
    with pytest.raises(ValueError, match=r"BLITZE_COUNT.*minimum"):
        CapabilitySetting(name="BLITZE_COUNT", default=0, minimum=30)
