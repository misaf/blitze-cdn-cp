"""The root wheel alone: what it offers, and what it must refuse."""

from __future__ import annotations

import json
import subprocess

import pytest

#: The `uv` this developer or this CI job is actually running, resolved once
#: rather than spelled as a bare name on every call. A partial path would be
#: whatever `PATH` happened to hold when a subprocess started, and these
#: subprocesses build and install wheels.
from lifecycle_support import (
    DETACHABLE_SITE_PACKAGES,
    LIFECYCLE_CAPABILITY,
    Environment,
    _uv,
)
from paths import optional_packages


def test_the_root_wheel_installs_no_optional_capability(core_only: Environment):
    """`pip install blitzecdn` is the control plane and nothing more.

    The extras in `[project.optional-dependencies]` name these distributions,
    which is how `blitzecdn[all]` works — and an extra a caller did not ask for
    must install nothing. If it did, "detached" would be unreachable from a
    normal install and the whole boundary would be decorative.
    """
    installed = _uv(
        "pip", "list", "--python", str(core_only.python), "--format", "json"
    )
    names = {entry["name"] for entry in json.loads(installed.stdout)}

    assert "blitzecdn" in names
    assert not {package.name for package in optional_packages()} & names


def test_core_alone_starts_and_registers_every_required_capability(
    core_only: Environment,
):
    """The control plane is coherent with no optional distribution present.

    Not "it imports": it discovers its plugins, builds its command tree and its
    router set, and reports nothing rejected. A required capability that had
    quietly moved out would be missing here rather than merely absent from a
    list somebody maintains.
    """
    report = core_only.report()

    assert report["rejected"] == []
    assert {"dns", "edges", "deployments", "tls", "diagnostics"} <= set(
        report["plugins"]  # type: ignore[arg-type]
    )
    assert report["commands"]
    assert report["routes"]


def test_core_alone_offers_no_optional_capability(core_only: Environment):
    report = core_only.report()

    assert LIFECYCLE_CAPABILITY not in report["capabilities"]
    assert LIFECYCLE_CAPABILITY not in report["commands"]
    assert "cache" not in report["capabilities"]
    assert not [path for path in report["routes"] if "cache" in path]  # type: ignore[union-attr]


def test_core_alone_loads_off_and_unmanaged_site_contracts(core_only: Environment):
    baseline = core_only.site_capabilities(
        {"compression": "off", "cache_enabled": False}
    )
    existing_tls = core_only.site_capabilities(
        {
            "compression": "off",
            "cache_enabled": False,
            "ssl_mode": "full",
            "ssl_automatic_mode": "custom",
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/example-com.pem",
            "certificate_key_path": "/etc/ssl/example-com.key",
        }
    )

    assert baseline["required"] == baseline["missing"] == []
    assert existing_tls["required"] == existing_tls["missing"] == []
    assert baseline["shape"] == existing_tls["shape"]


@pytest.mark.parametrize(
    ("_distribution", "capability", "overrides"),
    DETACHABLE_SITE_PACKAGES,
)
def test_core_alone_rejects_requested_site_capabilities(
    core_only: Environment,
    _distribution: str,
    capability: str,
    overrides: dict[str, object],
):
    result = core_only.site_capabilities(overrides)

    assert result["required"] == [capability]
    assert result["missing"] == [capability]


def test_the_api_and_the_cli_both_start_with_no_optional_package(
    core_only: Environment,
):
    """Both entry points compose from the registry, so both are worth asking.

    `--help` renders the whole command tree and `create_app` includes every
    contributed router, so a capability that core still expected to be there
    would fail here rather than at the first request for it.
    """
    finished = subprocess.run(
        [str(core_only.blitzecdn), "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    assert "deploy" in finished.stdout
    assert "backup" not in finished.stdout

    subprocess.run(
        [
            str(core_only.python),
            "-c",
            "from blitzecdn.api import create_app;"
            "from blitzecdn.core.config import Settings;"
            "create_app(Settings.from_environment()).openapi()",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
