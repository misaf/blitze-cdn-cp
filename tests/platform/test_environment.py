"""Application, host commands and backups agree on the environment-file format."""

import pytest

from blitzecdn.capabilities.backup.adapters.components import (
    _environment_assignments,
    _render_environment,
)
from blitzecdn.core.config.loading import read_environment_file
from blitzecdn.core.domain.environment import parse_environment
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.host.host_runtime import HostRuntimeError, ServiceDefinitions


def definitions(tmp_path):
    path = tmp_path / "compose.yml"
    path.write_text(
        "name: test\nservices:\n  api:\n    image: example:1\n"
        "    env_file: [runtime.env]\n"
    )
    return path


def test_all_consumers_agree_on_quotes_duplicates_and_literal_values(tmp_path):
    path = tmp_path / "runtime.env"
    path.write_text(
        '# comment\nexport BLITZE_FIRST = "quoted # value"\n'
        "BLITZE_FIRST=ignored\nBLITZE_EMPTY=\n"
        "BLITZE_LITERAL='${HOME} $HOME'\nBLITZE_NUMBER=0\n"
    )
    expected = {
        "BLITZE_FIRST": "quoted # value",
        "BLITZE_EMPTY": "",
        "BLITZE_LITERAL": "${HOME} $HOME",
        "BLITZE_NUMBER": "0",
    }
    assert read_environment_file(path) == expected
    host = ServiceDefinitions(definitions(tmp_path)).options("api")
    assert host["environment"] == expected
    archived = _render_environment(_environment_assignments(path.read_bytes()))
    assert parse_environment(archived.decode(), source="backup") == expected


@pytest.mark.parametrize("line", ["MISSING", "1NAME=value", "TOKEN=secret unquoted"])
def test_invalid_environment_is_refused_by_every_consumer(tmp_path, line):
    path = tmp_path / "runtime.env"
    path.write_text(line)
    with pytest.raises(ConfigurationError):
        read_environment_file(path)
    with pytest.raises(HostRuntimeError):
        ServiceDefinitions(definitions(tmp_path))
    with pytest.raises(ConfigurationError):
        _environment_assignments(path.read_bytes())


def test_malformed_secret_is_not_in_diagnostics(tmp_path):
    secret = "this-secret-must-stay-private"  # noqa: S105 - disposable parser fixture
    path = tmp_path / "runtime.env"
    path.write_text(f'TOKEN="{secret}')
    with pytest.raises(ConfigurationError) as error:
        read_environment_file(path)
    assert secret not in str(error.value)


def test_environment_symlinks_are_refused_even_when_dangling(tmp_path):
    path = tmp_path / "runtime.env"
    path.symlink_to(tmp_path / "absent")
    with pytest.raises(ConfigurationError, match="unsafe"):
        read_environment_file(path)
    with pytest.raises(HostRuntimeError):
        ServiceDefinitions(definitions(tmp_path))
