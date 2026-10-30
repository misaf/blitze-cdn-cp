"""Compose the site policy before any test module is imported.

A test module may build a site at import time, and a site's policy can only be
validated once the registry has composed it, so this cannot wait for a fixture.
"""

from __future__ import annotations

import pytest

from blitzecdn.composition import load_registry


def pytest_configure(config: pytest.Config) -> None:
    load_registry()
