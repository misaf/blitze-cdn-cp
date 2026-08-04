from pathlib import Path

import pytest

from blitzecdn.config import Settings
from blitzecdn.exceptions import ConfigurationError


def test_environment_configuration_and_precedence(tmp_path: Path):
    env = {
        "BLITZE_API_KEY": "a" * 32,
        "BLITZE_API_KEYS": f"alice:{'b' * 32}",
        "BLITZE_STATE_DIR": str(tmp_path / "custom-state"),
        "BLITZE_DEPLOYMENT_TIMEOUT_SECONDS": "120",
    }
    settings = Settings.from_environment(env, project_dir=tmp_path)
    assert set(settings.api_keys) == {"alice", "default"}
    assert settings.deployment_timeout_seconds == 120
    assert settings.state_dir == (tmp_path / "custom-state").resolve()


@pytest.mark.parametrize(
    "environment",
    [
        {"BLITZE_API_KEY": "short"},
        {"BLITZE_API_KEYS": "missing-separator"},
        {"BLITZE_DEPLOYMENT_TIMEOUT_SECONDS": "not-a-number"},
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


def test_project_toml_is_loaded_below_environment_precedence(tmp_path):
    (tmp_path / "blitzecdn.toml").write_text(
        "[blitzecdn]\nstate_dir = 'runtime'\ndeployment_timeout_seconds = 300\n",
        encoding="utf-8",
    )
    settings = Settings.from_environment(
        {"BLITZE_DEPLOYMENT_TIMEOUT_SECONDS": "120"}, project_dir=tmp_path
    )
    assert settings.state_dir == (tmp_path / "runtime").resolve()
    assert settings.deployment_timeout_seconds == 120


def test_project_toml_rejects_unknown_configuration(tmp_path):
    (tmp_path / "blitzecdn.toml").write_text(
        "[blitzecdn]\napi_secret = 'must-not-live-here'\n", encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match="unknown project configuration"):
        Settings.from_environment({}, project_dir=tmp_path)
