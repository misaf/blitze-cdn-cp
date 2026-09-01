"""Attaching and detaching a capability, through real Python packaging.

Everything else in the suite asserts against the source tree or against the
environment this run happens to have. This file builds real wheels, installs
them into throwaway virtualenvs, and asks the control plane what it can do —
because the property being claimed is about `pip install`, and a test that
mocked the registry would prove only that the mock was written correctly.

The established backup cycle uses three environments, each once per session:

* **core only** — the root wheel and nothing else. This is the configuration
  the acceptance criteria call "BlitzeCDN root package works alone", and it is
  also what proves the root wheel does not drag an optional distribution in
  behind it.
* **attached** — core plus one optional wheel, which must make that
  capability's metadata, routes and commands appear.
* **detached** — the attached environment with the optional wheel uninstalled
  again, which must return it to the first.

They are slow — a build and an install each — so they are marked `packaging`
and the whole module is skipped when the toolchain is not available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from paths import REPO_ROOT, optional_packages

#: The `uv` this developer or this CI job is actually running, resolved once
#: rather than spelled as a bare name on every call. A partial path would be
#: whatever `PATH` happened to hold when a subprocess started, and these
#: subprocesses build and install wheels.
UV = shutil.which("uv")

pytestmark = [
    pytest.mark.packaging,
    pytest.mark.skipif(UV is None, reason="packaging lifecycle needs the uv CLI"),
]

#: The capability used for the attach/detach cycle. One is enough: the
#: mechanism is the same for every optional distribution, and building a wheel
#: and two environments per package would multiply the slowest tests in the
#: suite for no additional property. `backup` is chosen because it is the
#: package with no runtime dependency on anything but `Settings`, so a failure
#: here is a failure of the *packaging*, never of the capability.
LIFECYCLE_PACKAGE = "blitzecdn-backup"
LIFECYCLE_CAPABILITY = "backup"

DETACHABLE_SITE_PACKAGES = (
    ("blitzecdn-compression", "compression", {"compression": "gzip"}),
    (
        "blitzecdn-certificates",
        "certificates",
        {
            "compression": "off",
            "ssl_mode": "full",
            "ssl_automatic_mode": "custom",
            "certificate_mode": "requested",
            "certificate_path": "/etc/blitzecdn/tls/cdn-example-com/fullchain.pem",
            "certificate_key_path": "/etc/blitzecdn/tls/cdn-example-com/privkey.pem",
        },
    ),
    (
        "blitzecdn-security",
        "security",
        {"compression": "off", "under_attack_mode": True},
    ),
    # HTTP/3 needs edge TLS, so the site has to serve it — with `existing`
    # material and a custom automatic mode, so that asking for HTTP/3 requires
    # `http3` and nothing else. HTTP/1.1 and HTTP/2 need no token at all, which
    # is the baseline the `off` case beside it covers.
    (
        "blitzecdn-http3",
        "http3",
        {
            "compression": "off",
            "ssl_mode": "full",
            "ssl_automatic_mode": "custom",
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/cdn-example-com.pem",
            "certificate_key_path": "/etc/ssl/cdn-example-com.key",
            "http3_enabled": True,
        },
    ),
    # The country visitor header on its own. `allowed_countries` would need
    # `security` as well, which is a real and separate requirement — covered
    # below, where two tokens is the property being asserted rather than noise
    # in a parametrisation about one.
    (
        "blitzecdn-geoip",
        "geoip",
        {"compression": "off", "visitor_headers": {"ip_country": True}},
    ),
)


def _uv(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["UV_CACHE_DIR"] = str(
        Path(tempfile.gettempdir()) / "blitzecdn-lifecycle-uv-cache"
    )
    # Never let the developer's own virtualenv answer for the one under test.
    environment.pop("VIRTUAL_ENV", None)
    return subprocess.run(
        [str(UV), *arguments],
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env=environment,
        timeout=900,
    )


@dataclass(frozen=True)
class Environment:
    """A throwaway virtualenv, and how to ask the control plane inside it."""

    root: Path

    @property
    def python(self) -> Path:
        return self.root / "bin" / "python"

    @property
    def blitzecdn(self) -> Path:
        return self.root / "bin" / "blitzecdn"

    def install(self, *wheels: Path) -> None:
        _uv("pip", "install", "--python", str(self.python), *map(str, wheels))

    def uninstall(self, *distributions: str) -> None:
        _uv("pip", "uninstall", "--python", str(self.python), *distributions)

    def report(self) -> dict[str, object]:
        """What the control plane in this environment says it consists of.

        Asked over a subprocess and answered as JSON, because the point is the
        *other* interpreter: importing the installed package into this one
        would read the workspace's own source tree through `sys.path` and
        report on an environment that is not the one being tested.
        """
        program = (
            "import json;"
            "from blitzecdn.core.plugins import load_plugins;"
            "r = load_plugins();"
            "print(json.dumps({"
            "'plugins': sorted(p.name for p in r.plugins),"
            "'capabilities': sorted(r.capabilities),"
            "'rejected': [str(x) for x in r.rejected],"
            "'commands': sorted("
            "  g.name or '' for g in r.cli_commands()),"
            "'routes': sorted("
            "  route.path for router in r.api_routers()"
            "  for route in router.routes if hasattr(route, 'path')),"
            "}))"
        )
        finished = subprocess.run(
            [str(self.python), "-c", program],
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
        return json.loads(finished.stdout)

    def site_capabilities(self, overrides: dict[str, object]) -> dict[str, object]:
        """Required and missing tokens for a real installed site schema."""
        program = (
            "import json,sys;"
            "from blitzecdn.core.plugins import load_plugins;"
            "from blitzecdn.features.sites import CdnSite;"
            "values={'name':'cdn-example-com',"
            "'server_names':['cdn.example.com'],"
            "'origin_host':'198.51.100.10',**json.loads(sys.argv[1])};"
            "site=CdnSite.model_validate(values);"
            "registry=load_plugins();"
            "print(json.dumps({"
            "'required':sorted(site.required_capabilities),"
            "'missing':list(registry.missing(site.required_capabilities)),"
            "'shape':sorted(site.model_dump(mode='json'))"
            "}))"
        )
        finished = subprocess.run(
            [str(self.python), "-c", program, json.dumps(overrides)],
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
        return json.loads(finished.stdout)

    def fleet_state(self, sites: list[dict[str, object]]) -> dict[str, object]:
        """The merged fleet desired state this installation would render.

        Through `registry.fleet_variables`, which is the call the deployment
        renderer makes — so the `overrides` claim is honoured here exactly as it
        is in a real run, and a collision between two plugins writing the same
        variable would raise rather than quietly pick one.
        """
        program = (
            "import json,sys;"
            "from blitzecdn.core.plugins import load_plugins;"
            "from blitzecdn.features.sites import CdnSite;"
            "sites=tuple(CdnSite.model_validate(v) for v in json.loads(sys.argv[1]));"
            "print(json.dumps(load_plugins().fleet_variables(sites, object())))"
        )
        finished = subprocess.run(
            [str(self.python), "-c", program, json.dumps(sites)],
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
        return json.loads(finished.stdout)


@pytest.fixture(scope="session")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Every distribution in the workspace, built.

    Building them all rather than only the two used below is deliberate: "all
    distributions build successfully" is itself one of the things being
    claimed, and a package that no longer builds should fail here rather than
    in a release.
    """
    output = tmp_path_factory.mktemp("wheels")
    _uv("build", "--wheel", "--out-dir", str(output))
    for package in optional_packages():
        _uv("build", "--wheel", "--package", package.name, "--out-dir", str(output))
    built = {
        path.name.split("-")[0].replace("_", "-"): path for path in output.glob("*.whl")
    }
    expected = {"blitzecdn", *(package.name for package in optional_packages())}
    assert expected <= set(built), f"missing wheels: {expected - set(built)}"
    return built


def _environment(root: Path) -> Environment:
    _uv(
        "venv",
        "--python",
        f"{sys.version_info.major}.{sys.version_info.minor}",
        str(root),
    )
    return Environment(root)


@pytest.fixture(scope="session")
def core_only(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
) -> Environment:
    environment = _environment(tmp_path_factory.mktemp("core-only") / "venv")
    environment.install(wheels["blitzecdn"])
    return environment


# --- core alone -------------------------------------------------------------


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
    assert {"sites", "dns", "edges", "deployments", "tls", "diagnostics"} <= set(
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
    baseline = core_only.site_capabilities({"compression": "off"})
    existing_tls = core_only.site_capabilities(
        {
            "compression": "off",
            "ssl_mode": "full",
            "ssl_automatic_mode": "custom",
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/cdn-example-com.pem",
            "certificate_key_path": "/etc/ssl/cdn-example-com.key",
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


# --- attach, then detach ----------------------------------------------------


@pytest.fixture(scope="session")
def attached(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
) -> Environment:
    """Core with one optional distribution installed, and the state before it.

    A fixture rather than a test that later tests build on, because the suite
    runs across workers: an assertion that depended on another test having
    already installed something would pass or fail on how the cases happened to
    be distributed. Each environment here is complete on its own.
    """
    environment = _environment(tmp_path_factory.mktemp("attached") / "venv")
    environment.install(wheels["blitzecdn"])
    before = environment.report()
    assert LIFECYCLE_CAPABILITY not in before["capabilities"], (
        "the core wheel installed an optional distribution"
    )
    environment.install(wheels[LIFECYCLE_PACKAGE])
    return environment


@pytest.fixture(scope="session")
def detached(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
) -> Environment:
    """The full round trip: core, attach, detach. Its own environment.

    Deliberately not the `attached` one with the package removed afterwards —
    that would make one fixture's state depend on another fixture's teardown
    order, which is the same fragility in a different place.
    """
    environment = _environment(tmp_path_factory.mktemp("detached") / "venv")
    environment.install(wheels["blitzecdn"], wheels[LIFECYCLE_PACKAGE])
    assert LIFECYCLE_CAPABILITY in environment.report()["capabilities"]
    environment.uninstall(LIFECYCLE_PACKAGE)
    return environment


def test_installing_a_distribution_makes_its_capability_appear(
    attached: Environment,
):
    """Attach: one `pip install`, and the capability is there.

    Nothing else changes. No line of core is edited, no registry is told, and
    the process is not even restarted with different arguments — the next one
    to start reads the installed metadata and finds an entry point that was not
    there before. The fixture asserts the capability was absent beforehand.
    """
    report = attached.report()

    assert LIFECYCLE_CAPABILITY in report["plugins"]
    assert LIFECYCLE_CAPABILITY in report["capabilities"]
    assert LIFECYCLE_CAPABILITY in report["commands"]
    assert report["rejected"] == []


def test_the_attached_capability_reaches_the_command_line(attached: Environment):
    """It is on the real command tree, not merely in a registry listing."""
    finished = subprocess.run(
        [str(attached.blitzecdn), "backup", "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    assert "create" in finished.stdout
    assert "restore" in finished.stdout


def test_an_optional_distribution_accepts_the_installed_core(attached: Environment):
    """Its declared dependency is satisfied by the core that is present.

    `uv pip check` is the honest form of this: it reads the installed metadata
    and reports an unsatisfied or conflicting requirement, which is what a
    version range that did not actually admit this core would produce.
    """
    _uv("pip", "check", "--python", str(attached.python))


def test_uninstalling_a_distribution_makes_its_capability_disappear(
    detached: Environment,
):
    """Detach: the capability goes, and everything else keeps working.

    The second half is the one worth stating. A control plane that lost `sites`
    along with `backup`, or that reported the removed package as *rejected*
    rather than absent, would both pass a test that only checked the capability
    was gone.
    """
    after = detached.report()

    assert LIFECYCLE_CAPABILITY not in after["plugins"]
    assert LIFECYCLE_CAPABILITY not in after["capabilities"]
    assert LIFECYCLE_CAPABILITY not in after["commands"]
    assert after["rejected"] == []
    assert {"sites", "dns", "edges", "deployments"} <= set(
        after["plugins"]  # type: ignore[arg-type]
    )
    assert after["routes"]


def test_the_cli_still_works_after_the_capability_is_detached(detached: Environment):
    finished = subprocess.run(
        [str(detached.blitzecdn), "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=300,
    )
    assert "deploy" in finished.stdout
    assert "backup" not in finished.stdout


@pytest.mark.parametrize(
    ("distribution", "capability", "overrides"),
    DETACHABLE_SITE_PACKAGES,
)
def test_site_capability_wheels_attach_and_detach_through_real_entry_points(
    tmp_path: Path,
    wheels: dict[str, Path],
    distribution: str,
    capability: str,
    overrides: dict[str, object],
):
    environment = _environment(tmp_path / "venv")
    environment.install(wheels["blitzecdn"])
    before = environment.report()
    assert capability not in before["capabilities"]
    assert environment.site_capabilities(overrides)["missing"] == [capability]

    environment.install(wheels[distribution])
    attached = environment.report()
    assert capability in attached["plugins"]
    assert capability in attached["capabilities"]
    assert attached["rejected"] == []
    assert environment.site_capabilities(overrides)["missing"] == []
    _uv("pip", "check", "--python", str(environment.python))

    if capability == "certificates":
        assert {"cert", "ssl"} <= set(attached["commands"])  # type: ignore[arg-type]
        assert any("certificates" in path for path in attached["routes"])  # type: ignore[union-attr]
        assert any("/ssl/automatic/" in path for path in attached["routes"])  # type: ignore[union-attr]

    environment.uninstall(distribution)
    detached_report = environment.report()
    assert capability not in detached_report["plugins"]
    assert capability not in detached_report["capabilities"]
    assert detached_report["rejected"] == []
    assert detached_report["routes"]
    assert environment.site_capabilities(overrides)["missing"] == [capability]
    if capability == "certificates":
        assert not {"cert", "ssl"} & set(detached_report["commands"])  # type: ignore[arg-type]
        assert not any("certificates" in path for path in detached_report["routes"])  # type: ignore[union-attr]
        assert not any("/ssl/automatic/" in path for path in detached_report["routes"])  # type: ignore[union-attr]


# --- configuration that names a capability nothing supplies -----------------


def test_configuration_requiring_an_absent_capability_fails_deterministically(
    core_only: Environment,
):
    """The deliberate half of "the package is not installed".

    Absence on its own is normal and silent — detaching is a supported
    operation. An installation that has *declared* it depends on a capability
    is a different case, and it refuses to start with the token named rather
    than coming up and behaving as though the capability had been configured
    off. Nothing in the path knows what `backup` is: the token comes from
    configuration and the answer from plugin metadata.
    """
    environment = dict(os.environ)
    environment.pop("VIRTUAL_ENV", None)
    environment["BLITZE_REQUIRED_CAPABILITIES"] = "backup"
    finished = subprocess.run(
        [str(core_only.blitzecdn), "plugins"],
        capture_output=True,
        text=True,
        env=environment,
        timeout=300,
    )

    assert finished.returncode != 0
    assert "backup" in finished.stderr + finished.stdout
    assert "no installed plugin provides" in finished.stderr + finished.stdout


# --- HTTP/3: an optional transport over a baseline that is not -------------


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
    assert {"http", "sites", "deployments"} <= set(after["plugins"])  # type: ignore[arg-type]
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
        "blitzecdn_http3/plugin.py",
    ]
    assert "Requires-Dist: blitzecdn" in requires


# --- GeoIP: one lookup capability behind two unrelated settings -------------


#: A site that asks the edge which country a visitor is in, one setting at a
#: time. The header case needs `geoip` alone; either firewall list needs
#: `security` for the rule and `geoip` for the lookup, which is the layering
#: the acceptance criteria call "a dependency on a capability token".
def _country_site(**policy: object) -> dict[str, object]:
    return {
        "name": "cdn-example-com",
        "server_names": ["cdn.example.com"],
        "origin_host": "198.51.100.10",
        "compression": "off",
        **policy,
    }


_COUNTRY_HEADER = _country_site(visitor_headers={"ip_country": True})
_ALLOWED_COUNTRIES = _country_site(firewall={"allowed_countries": ["DE", "FR"]})
_DENIED_COUNTRIES = _country_site(firewall={"denied_countries": ["RU"]})


def test_a_site_that_asks_for_no_country_needs_no_geoip(core_only: Environment):
    """The ordinary case: a CDN site works with this package absent.

    Not merely "it loads" — it requires no token at all, so nothing about it is
    conditional on a wheel. Source, method and path firewall rules are in here
    deliberately: they are the non-geographical half of the same policy block,
    and they must not drag the lookup in behind them.
    """
    plain = core_only.site_capabilities({"compression": "off"})
    filtered = core_only.site_capabilities(
        {
            "compression": "off",
            "visitor_headers": {"connecting_ip": True, "ip_country": False},
            "firewall": {
                "deny_sources": ["203.0.113.0/24"],
                "denied_methods": ["TRACE"],
            },
        }
    )

    assert plain["required"] == plain["missing"] == []
    assert filtered["required"] == ["security"]
    assert "geoip" not in filtered["missing"]


def test_core_alone_refuses_a_site_that_asks_for_the_country_header(
    core_only: Environment,
):
    """Detached, `BZ-IPCountry` is refused rather than quietly not written.

    An origin reading an absent header as "country unknown" is the failure
    this exists to prevent, and it is invisible: the site converges, the
    header never appears, and nothing reports it.
    """
    result = core_only.site_capabilities(_COUNTRY_HEADER)

    assert result["required"] == ["geoip"]
    assert result["missing"] == ["geoip"]


@pytest.mark.parametrize(
    "overrides", [_ALLOWED_COUNTRIES, _DENIED_COUNTRIES], ids=["allowed", "denied"]
)
def test_core_alone_refuses_a_site_that_asks_for_country_rules(
    core_only: Environment, overrides: dict[str, object]
):
    """Two tokens, because a country rule is a rule *and* a lookup.

    `security` owns the firewall and `geoip` owns resolving the address, and
    the site names both rather than one package answering for the other. The
    alternative — silently admitting every country a rule was meant to block —
    is a control an operator believes is in force and is not.
    """
    result = core_only.site_capabilities(overrides)

    assert result["required"] == ["geoip", "security"]
    assert result["missing"] == ["geoip", "security"]


def test_the_site_schema_is_identical_with_and_without_geoip_installed(
    core_only: Environment, attached: Environment
):
    """Country settings are core's, so the shape does not move with the wheel.

    A stored site asking for a country must load on a controller that has
    detached the capability — otherwise detaching would make a database row
    unreadable rather than a deployment refused.
    """
    detached_shape = core_only.site_capabilities(_COUNTRY_HEADER)
    attached_shape = attached.site_capabilities(_COUNTRY_HEADER)

    assert detached_shape["shape"] == attached_shape["shape"]
    assert {"visitor_headers", "firewall"} <= set(detached_shape["shape"])  # type: ignore[operator]


def test_attaching_geoip_makes_every_country_configuration_deployable(
    tmp_path: Path, wheels: dict[str, Path]
):
    """The full cycle for this capability, against real wheels.

    Both consumers at once, because "one capability, two consumers" is the
    packaging decision being made: one `pip install` has to satisfy the header
    and both firewall lists, and one `pip uninstall` has to refuse all three.
    """
    environment = _environment(tmp_path / "venv")
    environment.install(wheels["blitzecdn"], wheels["blitzecdn-security"])
    assert "geoip" not in environment.report()["capabilities"]
    for site in (_COUNTRY_HEADER, _ALLOWED_COUNTRIES, _DENIED_COUNTRIES):
        assert environment.site_capabilities(site)["missing"] == ["geoip"]

    environment.install(wheels["blitzecdn-geoip"])
    report = environment.report()
    assert "geoip" in report["plugins"]
    assert "geoip" in report["capabilities"]
    assert report["rejected"] == []
    for site in (_COUNTRY_HEADER, _ALLOWED_COUNTRIES, _DENIED_COUNTRIES):
        assert environment.site_capabilities(site)["missing"] == []
    _uv("pip", "check", "--python", str(environment.python))

    environment.uninstall("blitzecdn-geoip")
    after = environment.report()
    assert "geoip" not in after["capabilities"]
    assert after["rejected"] == []
    assert {"sites", "dns", "deployments", "http"} <= set(after["plugins"])  # type: ignore[arg-type]
    assert after["routes"]
    for site in (_COUNTRY_HEADER, _ALLOWED_COUNTRIES, _DENIED_COUNTRIES):
        assert environment.site_capabilities(site)["missing"] == ["geoip"]


def test_attaching_geoip_changes_no_desired_state(
    tmp_path: Path, wheels: dict[str, Path]
):
    """The extraction is architectural: the edge document does not move.

    Whether an edge resolves countries is fleet Ansible policy, not a variable
    the control plane derives, so this package contributes none — and the fleet
    document a country-aware installation renders is byte-identical before and
    after it is attached.
    """
    fleet = [
        _COUNTRY_HEADER,
        _country_site(name="beta", server_names=["b.example.com"]),
    ]
    environment = _environment(tmp_path / "venv")
    environment.install(wheels["blitzecdn"])
    before = environment.fleet_state(fleet)

    environment.install(wheels["blitzecdn-geoip"])

    assert environment.fleet_state(fleet) == before


def test_the_root_wheel_neither_contains_nor_requires_geoip(wheels: dict[str, Path]):
    """The dependency arrow, read off the built artefacts themselves."""
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn"]) as archive:
        names = archive.namelist()
        metadata = next(name for name in names if name.endswith("METADATA"))
        requires = archive.read(metadata).decode()

    assert "blitzecdn_geoip" not in "".join(names)

    mentions = [
        line.replace("'", "").replace('"', "")
        for line in requires.splitlines()
        if line.startswith("Requires-Dist:") and "blitzecdn-geoip" in line
    ]
    assert mentions, "the root wheel does not offer the geoip extra at all"
    assert all(
        "extra == geoip" in line or "extra == all" in line for line in mentions
    ), mentions


def test_the_geoip_wheel_contains_only_its_own_package(wheels: dict[str, Path]):
    """No vendored core, and nothing but the module and its metadata."""
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn-geoip"]) as archive:
        modules = sorted(name for name in archive.namelist() if name.endswith(".py"))
        metadata = next(
            name for name in archive.namelist() if name.endswith("METADATA")
        )
        requires = archive.read(metadata).decode()

    assert modules == [
        "blitzecdn_geoip/__init__.py",
        "blitzecdn_geoip/plugin.py",
    ]
    assert "Requires-Dist: blitzecdn" in requires
