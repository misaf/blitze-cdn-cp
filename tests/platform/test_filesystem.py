import os

import pytest
import yaml

from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.runtime.filesystem import atomic_write_yaml


def test_atomic_yaml_is_restrictive(tmp_path):
    target = tmp_path / "nested/state.yml"
    atomic_write_yaml(target, {"secret": "masked"})
    assert yaml.safe_load(target.read_text()) == {"secret": "masked"}
    assert os.stat(target).st_mode & 0o777 == 0o600


def test_atomic_yaml_refuses_symlink(tmp_path):
    real = tmp_path / "real"
    real.write_text("safe", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ConfigurationError, match="symlink"):
        atomic_write_yaml(link, {"bad": True})
    assert real.read_text(encoding="utf-8") == "safe"
