"""HTTP/3: an optional transport over a baseline that is not."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

#: The `uv` this developer or this CI job is actually running, resolved once
#: rather than spelled as a bare name on every call. A partial path would be
#: whatever `PATH` happened to hold when a subprocess started, and these
#: subprocesses build and install wheels.
from lifecycle_support import (
    ANSIBLE_PACKAGE,
    Environment,
    _environment,
    _uv,
)


#: One site serving edge TLS, so `http3_enabled` is allowed to be true. The
#: `existing` certificate mode keeps it from requiring `certificates` as well,
#: which would make the assertions below about two capabilities at once.
def _http3_site(name: str, *, http3: bool, enabled: bool = True) -> dict[str, object]:
    return {
        "name": name,
        "server_names": [f"{name}.example.com"],
        "origin_host": "198.51.100.10",
        "enabled": enabled,
        "compression": "off",
        "cache_enabled": False,
        "ssl_mode": "full",
        "ssl_automatic_mode": "custom",
        "certificate_mode": "existing",
        "certificate_path": f"/etc/ssl/{name}.pem",
        "certificate_key_path": f"/etc/ssl/{name}.key",
        "http3_enabled": http3,
    }


def test_baseline_http_needs_no_optional_distribution(core_only: Environment):
    """HTTP/1.1 and HTTP/2 are not a capability anybody attaches.

    `http` registers, it is required, and a site that has not asked for HTTP/3
    needs no token at all — which is what "baseline" has to mean if extracting
    HTTP/3 did not quietly make ordinary sites depend on a package.
    """
    report = core_only.report()

    assert "http" in report["plugins"]
    assert "http3" not in report["capabilities"]
    assert (
        core_only.site_capabilities(_http3_site("alpha", http3=False))["required"] == []
    )


def test_core_alone_still_renders_the_quic_listener_variables(core_only: Environment):
    """Detached, the document keeps its shape and states the off position.

    Both variables are `required: true` in the edge role's argument spec, so a
    control plane that simply stopped emitting them when the distribution was
    absent would be a different contract with Ansible depending on what was
    installed. Core writes the baseline; `blitzecdn-http3` overrides it.
    """
    state = core_only.fleet_state(
        [_http3_site("alpha", http3=False), _http3_site("bravo", http3=False)]
    )

    assert state["blitzecdn_edge_http3_enabled"] is False
    assert state["blitzecdn_nginx_http3_listener_owner"] == ""


def test_core_alone_refuses_a_site_that_asks_for_http3(core_only: Environment):
    """The one intended semantic change of the whole extraction.

    Not a silent downgrade to HTTP/2 and not an ignored setting: the site loads
    — the field is core's — and the token it needs is reported missing, which
    is what turns into a blocking validation issue before any playbook runs.
    """
    result = core_only.site_capabilities(_http3_site("alpha", http3=True))

    assert result["required"] == ["http3"]
    assert result["missing"] == ["http3"]


def test_the_site_schema_is_identical_with_and_without_http3_installed(
    core_only: Environment, attached: Environment
):
    """`CdnSite` does not change shape when a distribution is installed.

    The persisted policy JSON, the API schemas and the deployment snapshots are
    all this shape, so a capability that added or removed a field on being
    attached would make stored state unreadable in the other configuration.
    """
    detached_shape = core_only.site_capabilities(_http3_site("alpha", http3=False))
    attached_shape = attached.site_capabilities(_http3_site("alpha", http3=False))

    assert detached_shape["shape"] == attached_shape["shape"]
    assert "http3_enabled" in detached_shape["shape"]


def test_attaching_http3_makes_the_capability_and_its_fleet_state_appear(
    tmp_path: Path, wheels: dict[str, Path]
):
    """The full cycle for this capability, against real wheels.

    Asserted on the *merged* document rather than on one plugin's contribution,
    because the property is that installing the distribution changes what the
    edge is asked to do — and that the two plugins writing these two variables
    do not collide when both are present.
    """
    fleet = [_http3_site("bravo", http3=True), _http3_site("alpha", http3=True)]
    environment = _environment(tmp_path / "venv")
    environment.install(wheels["blitzecdn"])

    assert environment.fleet_state(fleet) == {
        "blitzecdn_edge_http3_enabled": False,
        "blitzecdn_nginx_http3_listener_owner": "",
    }

    environment.install(wheels["blitzecdn-http3"])
    report = environment.report()
    assert "http3" in report["plugins"]
    assert "http3" in report["capabilities"]
    assert report["rejected"] == []
    assert environment.site_capabilities(_http3_site("a", http3=True))["missing"] == []
    # Sorted by name, so `alpha` owns reuseport though `bravo` came first.
    assert environment.fleet_state(fleet) == {
        "blitzecdn_edge_http3_enabled": True,
        "blitzecdn_nginx_http3_listener_owner": "alpha",
    }
    _uv("pip", "check", "--python", str(environment.python))

    environment.uninstall("blitzecdn-http3")
    after = environment.report()
    assert "http3" not in after["capabilities"]
    assert after["rejected"] == []
    assert {"http", "dns", "deployments"} <= set(after["plugins"])
    assert environment.fleet_state(fleet) == {
        "blitzecdn_edge_http3_enabled": False,
        "blitzecdn_nginx_http3_listener_owner": "",
    }


def test_attaching_http3_to_a_fleet_that_wants_none_changes_nothing(
    tmp_path: Path, wheels: dict[str, Path]
):
    """Installed is not enabled, checked at the edge document.

    An operator who attaches the distribution before turning HTTP/3 on
    anywhere must converge exactly what they converged before.
    """
    fleet = [_http3_site("alpha", http3=False)]
    environment = _environment(tmp_path / "venv")
    environment.install(wheels["blitzecdn"])
    before = environment.fleet_state(fleet)

    environment.install(wheels["blitzecdn-http3"])

    assert environment.fleet_state(fleet) == before


def test_the_root_wheel_neither_contains_nor_requires_http3(wheels: dict[str, Path]):
    """The dependency arrow, read off the built artefacts themselves.

    A root wheel that shipped the module would make the capability
    undetachable; one that required the distribution would make it
    uninstallable-without. Both are invisible in the source tree and obvious
    here.
    """
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn"]) as archive:
        names = archive.namelist()
        metadata = next(name for name in names if name.endswith("METADATA"))
        requires = archive.read(metadata).decode()

    assert not [name for name in names if name.startswith("blitzecdn_http3")]
    assert "blitzecdn_http3" not in "".join(names)

    # Named only behind the extras that ask for it, and never unconditionally.
    # Quote style is the backend's business, so the marker is compared with
    # quotes stripped rather than spelled the way this build happens to emit it.
    mentions = [
        line.replace("'", "").replace('"', "")
        for line in requires.splitlines()
        if line.startswith("Requires-Dist:") and "blitzecdn-http3" in line
    ]
    assert mentions, "the root wheel does not offer the http3 extra at all"
    assert all(
        "extra == http3" in line or "extra == all" in line for line in mentions
    ), mentions


def test_the_http3_wheel_contains_only_its_own_package(wheels: dict[str, Path]):
    """No vendored core, and nothing but the module and its metadata."""
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn-http3"]) as archive:
        modules = sorted(name for name in archive.namelist() if name.endswith(".py"))
        metadata = next(
            name for name in archive.namelist() if name.endswith("METADATA")
        )
        requires = archive.read(metadata).decode()

    assert modules == [
        "blitzecdn_http3/__init__.py",
        "blitzecdn_http3/ansible/__init__.py",
        "blitzecdn_http3/plugin.py",
    ]
    assert "Requires-Dist: blitzecdn" in requires


def test_validate_names_the_capability_a_configuration_asks_for_and_lacks(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
):
    """`blitzecdn validate`, before and after the wheel, in one environment.

    The acceptance criterion spelled as the command an operator types.
    Detached, the run refuses and names the token; attached, that reason is
    gone — the command still has nothing to converge in a bare virtualenv, so
    what is asserted is the *disappearance of this reason*, not a green run
    against a fleet that does not exist.
    """
    installation = _environment(tmp_path_factory.mktemp("validate") / "venv")
    installation.install(wheels["blitzecdn"])
    environment = dict(os.environ)
    environment.pop("VIRTUAL_ENV", None)
    environment["BLITZE_REQUIRED_CAPABILITIES"] = "cache"

    def validate() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(installation.blitzecdn), "validate"],
            capture_output=True,
            text=True,
            cwd=installation.root,
            env=environment,
            timeout=300,
        )

    detached = validate()
    assert detached.returncode != 0
    assert "cache" in detached.stdout + detached.stderr

    installation.install(wheels[ANSIBLE_PACKAGE])
    attached = validate()
    assert "requires the capability cache" not in attached.stdout + attached.stderr
