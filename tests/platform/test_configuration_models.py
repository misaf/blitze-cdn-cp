"""Configuration behavior at its real loading and composition boundaries."""

from pathlib import Path

import pytest
from pydantic import Field, SecretStr

from blitzecdn.capabilities.backup.settings import BackupConfig
from blitzecdn.capabilities.security.settings import SecurityConfig
from blitzecdn.capabilities.tls.settings import CertificateConfig
from blitzecdn.composition import load_registry
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.features import ConfigurationModel, resolve_capability_environment


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
