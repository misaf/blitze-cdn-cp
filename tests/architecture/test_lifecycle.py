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
import tomllib
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
    (
        "blitzecdn-compression",
        "compression",
        {"compression": "gzip", "cache_enabled": False},
    ),
    (
        "blitzecdn-cache",
        "cache",
        {"compression": "off", "cache_enabled": True},
    ),
    (
        "blitzecdn-certificates",
        "certificates",
        {
            "compression": "off",
            "cache_enabled": False,
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
        {"compression": "off", "cache_enabled": False, "under_attack_mode": True},
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
            "cache_enabled": False,
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
        {
            "compression": "off",
            "cache_enabled": False,
            "visitor_headers": {"ip_country": True},
        },
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

    def ansible_roles(self) -> dict[str, object]:
        """Which role directories this installation would give Ansible.

        Resolved the way a deployment resolves it — the registry's
        contributions through `resolve_role_search_path` — and reported as
        paths and role names, so the assertion can be about what is installed
        rather than about what the source tree happens to contain.
        """
        program = (
            "import json;"
            "from pathlib import Path;"
            "from blitzecdn.core.plugins import load_plugins;"
            "from blitzecdn.core.plugins.resolution import ("
            "  resolve_edge_capability_roles, resolve_host_capability_roles,"
            "  resolve_role_search_path, resolve_teardown_capability_roles);"
            "from blitzecdn.core.plugins.resolution import ("
            "  resolve_edge_modules, resolve_nginx_resources);"
            # The platform's own roles, from the installed distribution. This
            # used to be a fabricated path, because core resolved its tree from
            # the checkout and there was nothing to point at in a virtualenv.
            "from blitzecdn.ansible import ROLES_PATH as core;"
            "path = resolve_role_search_path(core, load_plugins()"
            ".ansible_contributions());"
            "print(json.dumps({"
            "'paths': [str(p) for p in path],"
            "'roles': sorted(r.name for p in path if p.is_dir()"
            "  for r in p.iterdir() if r.is_dir()),"
            # And which of those roles the edge play would run, resolved the
            # same way and from the same contributions. Two questions with one
            # source: a package may ship a role only its own plays reach.
            "'edge_roles': list(resolve_edge_capability_roles("
            "  load_plugins().ansible_contributions())),"
            # And the play's other slot, which is a separate list because it is
            # a separate position in the play: what a capability does to the
            # host once the edge is already serving.
            "'host_roles': list(resolve_host_capability_roles("
            "  load_plugins().ansible_contributions())),"
            # And the decommission play's slot, which is the one a capability
            # uses to take its own files off a host that is leaving.
            "'teardown_roles': list(resolve_teardown_capability_roles("
            "  load_plugins().ansible_contributions())),"
            # And the Nginx dynamic modules those roles' configuration needs
            # loaded. The image is built from this list and the edge renders
            # its own from it, so a detached capability whose module still
            # appeared here would be an edge loading it forever.
            "'modules': [[m.plugin, m.name] for m in resolve_edge_modules("
            "  load_plugins().ansible_contributions())],"
            "'nginx': {context:[{'plugin':r.plugin,'name':r.name,"
            "  'exists':r.template.is_file()} for r in resources]"
            "  for context,resources in resolve_nginx_resources("
            "    load_plugins().nginx_contributions()).items()},"
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
            "from blitzecdn.capabilities.sites import CdnSite;"
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
            "from blitzecdn.capabilities.sites import CdnSite;"
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


def _declared_workspace_wheels(
    distribution: str, wheels: dict[str, Path]
) -> tuple[Path, ...]:
    """The wheels a distribution's own metadata says it needs beside `blitzecdn`.

    One package declares another today: `blitzecdn-certificates` runs
    `blitzecdn-origins`' play for the Automatic SSL/TLS scan. Resolved from the
    manifest rather than hard-coded, so the next declared edge is installed
    here without this file being edited — and so `uv pip install` really
    resolves the requirement instead of the test quietly working around it.
    """
    manifest = tomllib.loads(
        (REPO_ROOT / "packages" / distribution / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    names = [
        requirement.split(">")[0].split("[")[0].strip()
        for requirement in manifest["project"]["dependencies"]
    ]
    return tuple(wheels[name] for name in names if name != "blitzecdn")


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

    environment.install(
        wheels[distribution], *_declared_workspace_wheels(distribution, wheels)
    )
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


# --- the deployment implementation attaches and detaches with the wheel -----


#: The platform roles: the ones that exist on every edge because the control
#: plane converged it, not because a capability was attached. They are core's
#: half of the same contract every package under `packages/` already keeps —
#: the deployment implementation ships inside the distribution that asks for
#: it — and the two tests below are the capability tests above, turned on core.
PLATFORM_ROLES = (
    "blitzecdn_base",
    "blitzecdn_capabilities",
    "blitzecdn_controlplane",
    "blitzecdn_docker",
    "blitzecdn_edge",
    "blitzecdn_edge_stack",
    "blitzecdn_firewall",
    "blitzecdn_kernel",
    "blitzecdn_nginx",
    "blitzecdn_teardown",
    "blitzecdn_uninstall",
)

#: The plays core passes to `run_playbook` by path, plus the two it hands to
#: Ansible as configuration. The inventory plugin is in the list because it is
#: the piece with no capability precedent: Ansible loads inventory plugins by
#: *directory*, so a wheel that carried the roles and left the plugin in the
#: checkout would resolve every role and then find no fleet to run them on.
PLATFORM_PLAYBOOKS = (
    "control-plane.yml",
    "decommission.yml",
    "edge.yml",
    "uninstall.yml",
)

#: The capability whose Ansible really is its own: two roles and two plays that
#: exist only while `blitzecdn-cache` is installed. `backup` drives the generic
#: attach/detach cycle above because it has no Ansible at all, which is exactly
#: why it cannot drive this one.
ANSIBLE_PACKAGE = "blitzecdn-cache"
ANSIBLE_ROLES = (
    "blitzecdn_cache",
    "blitzecdn_cache_config",
    "blitzecdn_stats",
)

#: The capability whose role core's *edge play* runs, which is the other half
#: of the Ansible contribution and a second independent capability role.
EDGE_ROLE_PACKAGE = "blitzecdn-geoip"
EDGE_ROLE = "blitzecdn_geoip"

#: And the capability that fills the play's *host* slot instead. A third
#: package in the same environment, because the property worth proving is that
#: the slots are composed independently: this one contributes no edge role and
#: no Nginx resource, and its roles must still arrive — in the host list, never
#: in the edge one.
#:
#: It pairs its host roles with a teardown role, so it reaches the decommission
#: slot too. That pairing is the point rather than an extra: the two files it
#: writes are at paths only this wheel knows, and core's `blitzecdn_teardown`
#: used to name both — which is the leak the third slot exists to close.
HOST_ROLE_PACKAGE = "blitzecdn-hardening"
HOST_ROLES = ("blitzecdn_sshd", "blitzecdn_fail2ban")
HOST_TEARDOWN_ROLE = "blitzecdn_hardening_teardown"

#: And the capability that fills two slots at once, one of them in a different
#: play. `blitzecdn-resolver` converges in the *edge* slot and withdraws in the
#: decommission slot, which is the other shape the pairing takes: `hardening`
#: converges in the host slot and withdraws in the same decommission one. Two
#: packages reaching that slot from different halves of the edge play is what
#: makes it visible that core composes the list rather than either of them.
TEARDOWN_ROLE_PACKAGE = "blitzecdn-resolver"
TEARDOWN_EDGE_ROLE = "blitzecdn_resolver"
TEARDOWN_ROLE = "blitzecdn_resolver_teardown"

#: Both halves of the decommission slot, in the order core composes it: sorted
#: by plugin name, so `hardening` precedes `resolver`. Removal order is not the
#: reverse of convergence order and does not need to be — each role withdraws
#: only what its own package wrote.
TEARDOWN_ROLES = (HOST_TEARDOWN_ROLE, TEARDOWN_ROLE)


@pytest.fixture(scope="session")
def ansible_cycle(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
) -> dict[str, dict[str, object]]:
    """One environment, read at all three points of the cycle.

    Three subprocess reports rather than three virtualenvs: what is being
    claimed is that the *same* installation answers differently before, during
    and after, so building a separate environment per state would prove less
    at three times the cost.
    """
    environment = _environment(tmp_path_factory.mktemp("ansible-cycle") / "venv")
    environment.install(wheels["blitzecdn"])
    before = environment.ansible_roles()
    environment.install(
        wheels[ANSIBLE_PACKAGE],
        wheels[EDGE_ROLE_PACKAGE],
        wheels[HOST_ROLE_PACKAGE],
        wheels[TEARDOWN_ROLE_PACKAGE],
    )
    attached = environment.ansible_roles()
    environment.uninstall(
        ANSIBLE_PACKAGE,
        EDGE_ROLE_PACKAGE,
        HOST_ROLE_PACKAGE,
        TEARDOWN_ROLE_PACKAGE,
    )
    return {
        "before": before,
        "attached": attached,
        "after": environment.ansible_roles(),
    }


def test_core_alone_offers_no_capability_owned_role(
    ansible_cycle: dict[str, dict[str, object]],
):
    """The root wheel carries the platform's roles and nobody else's.

    The search path is core's directory and nothing beside it, because there is
    no contributed directory to add — and that directory is now inside the
    installed distribution, so the roles it holds are the platform's eleven
    rather than the empty list a fabricated path used to produce.
    """
    assert sorted(PLATFORM_ROLES) == ansible_cycle["before"]["roles"]
    assert len(ansible_cycle["before"]["paths"]) == 1
    assert "site-packages" in ansible_cycle["before"]["paths"][0]
    # And the edge play converges nothing beyond the platform. A core-only
    # installation renders `blitzecdn_capability_roles` as an empty list, which
    # is the shape the play's include loops over — not a missing variable it
    # would have to defend against.
    assert ansible_cycle["before"]["edge_roles"] == []
    assert ansible_cycle["before"]["host_roles"] == []
    assert ansible_cycle["before"]["teardown_roles"] == []
    # And it loads no dynamic module. Everything core renders — HTTP/1.1,
    # HTTP/2, HTTP/3, TLS, proxying, gzip — is compiled into Nginx, so a
    # capability-free edge has an empty `load_module` list rather than a
    # baseline one.
    assert ansible_cycle["before"]["modules"] == []
    assert not any(ansible_cycle["before"]["nginx"].values())


def test_installing_a_distribution_makes_its_roles_resolvable(
    ansible_cycle: dict[str, dict[str, object]],
):
    """Attach, on the Ansible side. One `pip install` and the roles are there.

    And they are there *inside the installed distribution* — the asserted path
    is under the virtualenv, so this cannot be passing because the repository
    checkout happens to be on disk beside it.
    """
    attached = ansible_cycle["attached"]

    assert (
        sorted(
            [
                # The platform's, which are there in every state of the cycle
                # because they ship in the root wheel.
                *PLATFORM_ROLES,
                *ANSIBLE_ROLES,
                EDGE_ROLE,
                *HOST_ROLES,
                TEARDOWN_EDGE_ROLE,
                *TEARDOWN_ROLES,
            ]
        )
        == attached["roles"]
    )
    contributed = attached["paths"][1]
    assert "site-packages" in contributed
    assert "blitzecdn_cache/ansible/roles" in contributed
    assert str(REPO_ROOT) not in contributed
    nginx = attached["nginx"]
    assert {resource["plugin"] for values in nginx.values() for resource in values} == {
        "cache",
        "geoip",
    }
    assert all(resource["exists"] for values in nginx.values() for resource in values)


def test_installing_a_distribution_puts_its_role_into_the_edge_play(
    ansible_cycle: dict[str, dict[str, object]],
):
    """Attach, all the way to what a deploy actually converges.

    Resolving the role by name is not enough on its own: a role nothing
    includes changes no edge. The contribution carries both halves, so
    installing the wheel is what puts `blitzecdn_geoip` in the list core's edge
    play loops over — and `blitzecdn-cache`, installed in the same environment,
    contributes roles its own plays reach and nothing to the play, which is
    what makes the two halves visibly separate.
    """
    assert ansible_cycle["attached"]["edge_roles"] == [
        "blitzecdn_cache_config",
        EDGE_ROLE,
        TEARDOWN_EDGE_ROLE,
    ]


def test_installing_a_distribution_puts_its_module_into_the_edge_s_load_list(
    ansible_cycle: dict[str, dict[str, object]],
):
    """Attach, all the way to what the running Nginx loads.

    The last place a capability used to survive its own removal. `geoip2` was
    built into the edge image and loaded by a file inside the image, so an
    installation with no `blitzecdn-geoip` still loaded the module on every
    edge — the image is built once and pinned by digest, and nothing about
    detaching a distribution could reach it.

    Declaring the module with the capability makes the `load_module` list a
    function of what is installed: `blitzecdn-cache`, `blitzecdn-hardening` and
    `blitzecdn-resolver` are in this same environment and need none, and the
    module named here is named by exactly one wheel.
    """
    assert ansible_cycle["attached"]["modules"] == [["geoip", "geoip2"]]


def test_installing_a_host_capability_fills_the_other_slot_only(
    ansible_cycle: dict[str, dict[str, object]],
):
    """The two slots are composed independently, from one set of contributions.

    `blitzecdn-hardening` declares `host_roles` and `teardown_roles` and
    nothing else: no edge role, no Nginx resource, no environment key, no
    desired-state variable. So its converging roles must appear in the list the
    play's host slot loops over and in neither of the others — a package
    landing in the edge slot instead would run SSH hardening before the
    firewall was validated, which is how a host ends up key-only and
    unreachable at once. Its withdrawal is a third role and is asserted
    separately, below.

    Declared order inside the contribution is kept, because that package alone
    owns both roles and Fail2Ban's jail has to protect a daemon that has
    already stopped accepting passwords.
    """
    attached = ansible_cycle["attached"]

    assert attached["host_roles"] == list(HOST_ROLES)
    assert not set(HOST_ROLES) & set(attached["edge_roles"])
    assert "hardening" not in {
        resource["plugin"]
        for values in attached["nginx"].values()
        for resource in values
    }


def test_a_capability_that_writes_outside_core_s_trees_can_take_it_off_again(
    ansible_cycle: dict[str, dict[str, object]],
):
    """The decommission slot, and the pairing it exists for.

    `blitzecdn-resolver` writes a drop-in under /etc/systemd/resolved.conf.d;
    `blitzecdn-hardening` writes an SSH policy under /etc/ssh/sshd_config.d and
    a jail under /etc/fail2ban/jail.d. Core's `blitzecdn_teardown` removes the
    trees it wrote, the shared runtime directories and every systemd unit
    matching the managed prefix — none of those three files is any of those,
    and core naming one would put a path belonging to a wheel into a role that
    is installed whether or not the wheel is. It did name two of them, which is
    what this pair of packages between them now takes out of core.

    So the removal travels with the capability, in a slot of its own, and both
    packages reach that slot from a different half of the edge play: resolver
    converges in the edge slot, hardening in the host slot. Whichever half a
    capability converges from, its withdrawal lands here — and never in either
    of the other two, since a teardown role in the edge slot would strip
    resolution, or host access, from every edge on every deploy.
    """
    attached = ansible_cycle["attached"]

    # Sorted by plugin name, like every slot: two independent packages, one
    # list, composed by core rather than by either of them.
    assert attached["teardown_roles"] == list(TEARDOWN_ROLES)
    for role in TEARDOWN_ROLES:
        assert role not in attached["edge_roles"]
        assert role not in attached["host_roles"]
    # And neither package's *converging* roles leak the other way. Withdrawing
    # is a separate role in both cases, which is what keeps a decommission from
    # re-converging the very policy it is removing.
    assert TEARDOWN_EDGE_ROLE not in attached["teardown_roles"]
    assert not set(HOST_ROLES) & set(attached["teardown_roles"])
    # `blitzecdn-cache` and `blitzecdn-geoip` are installed in this same
    # environment and reach this slot with nothing: a capability that writes
    # only inside the trees core already removes declares no teardown role, and
    # absence here is the correct answer rather than an omission.
    assert not {*ANSIBLE_ROLES, EDGE_ROLE} & set(attached["teardown_roles"])


def test_uninstalling_a_distribution_takes_its_roles_with_it(
    ansible_cycle: dict[str, dict[str, object]],
):
    """Detach, on the Ansible side, and the acceptance criterion in full.

    The Python capability and its deployment implementation leave together.
    Nothing in core is edited, no directory is pruned by hand, and the role
    names simply stop resolving on the next run. The edge play stops running
    the capability's role in the same breath, which is the part that means an
    already-converged edge is left alone rather than half-managed.
    """
    assert ansible_cycle["after"] == ansible_cycle["before"]
    assert ansible_cycle["after"]["edge_roles"] == []
    assert ansible_cycle["after"]["host_roles"] == []
    # Including the removal role. A capability that leaves takes its teardown
    # with it, which is why core's own teardown may not depend on one having
    # ever been installed.
    assert ansible_cycle["after"]["teardown_roles"] == []
    # And the module goes with it. This is the half the edge image used to get
    # wrong on its own: the role and the Nginx resources disappeared while the
    # module kept loading, because the image had been built naming it.
    assert ansible_cycle["after"]["modules"] == []
    assert not any(ansible_cycle["after"]["nginx"].values())


def test_the_capability_wheel_carries_its_whole_ansible_tree(
    wheels: dict[str, Path],
):
    """Built, not assumed. An editable install would hide the real failure.

    A wheel that shipped only the `.py` files would leave a plugin pointing at
    a directory that does not exist on an installed controller, and every
    purge would fail with "the role was not found" — on the controller, never
    in this checkout.
    """
    import zipfile

    with zipfile.ZipFile(wheels[ANSIBLE_PACKAGE]) as archive:
        names = set(archive.namelist())

    root = "blitzecdn_cache/ansible"
    for role in ANSIBLE_ROLES:
        assert f"{root}/roles/{role}/tasks/main.yml" in names
        assert f"{root}/roles/{role}/defaults/main.yml" in names
        assert f"{root}/roles/{role}/meta/argument_specs.yml" in names
    # Not only YAML: the statistics role reads the access log with a shipped
    # script, and a build that filtered by extension would drop it silently.
    assert f"{root}/roles/blitzecdn_stats/files/collect-cache-stats.sh" in names
    assert f"{root}/playbooks/cache-purge.yml" in names
    assert f"{root}/playbooks/stats.yml" in names


def test_the_root_wheel_carries_no_capability_owned_ansible(
    wheels: dict[str, Path],
):
    """The other direction: core's tree kept nothing behind when they moved."""
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn"]) as archive:
        names = "".join(archive.namelist())

    assert "blitzecdn_cache" not in names
    assert "blitzecdn_stats" not in names
    assert "cache-purge.yml" not in names
    assert "acme-challenge.yml" not in names


@pytest.mark.parametrize(
    ("distribution", "module", "resources"),
    [
        (
            "blitzecdn-cache",
            "blitzecdn_cache",
            {"cache-http.conf.j2", "cache-upstream.conf.j2"},
        ),
        (
            "blitzecdn-compression",
            "blitzecdn_compression",
            {"compression-server.conf.j2"},
        ),
        (
            "blitzecdn-http3",
            "blitzecdn_http3",
            {"http3-server.conf.j2", "http3-upstream.conf.j2"},
        ),
        (
            "blitzecdn-geoip",
            "blitzecdn_geoip",
            {"geoip-http.conf.j2", "geoip-upstream.conf.j2"},
        ),
        (
            "blitzecdn-security",
            "blitzecdn_security",
            {
                "security-http.conf.j2",
                "security-server.conf.j2",
                "security-access.conf.j2",
                "security-upstream.conf.j2",
            },
        ),
    ],
)
def test_capability_wheels_carry_their_nginx_resources(
    wheels: dict[str, Path],
    distribution: str,
    module: str,
    resources: set[str],
):
    import zipfile

    with zipfile.ZipFile(wheels[distribution]) as archive:
        names = set(archive.namelist())

    assert {f"{module}/nginx/{resource}" for resource in resources} <= names


def test_an_installed_capability_locates_its_plays_without_the_repository(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
):
    """Resource discovery, asked of a real install from outside the checkout.

    The subprocess runs with its working directory somewhere else entirely, so
    a path built from `cwd` or from a repository-relative walk would fail here
    and only here.
    """
    environment = _environment(tmp_path_factory.mktemp("resources") / "venv")
    environment.install(wheels["blitzecdn"], wheels[ANSIBLE_PACKAGE])
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    program = (
        "import json;"
        "from blitzecdn_cache import ansible;"
        "print(json.dumps({"
        "'purge': str(ansible.CACHE_PURGE_PLAYBOOK),"
        "'exists': ansible.CACHE_PURGE_PLAYBOOK.is_file()"
        " and ansible.STATS_PLAYBOOK.is_file()"
        " and (ansible.ROLES_PATH / 'blitzecdn_cache').is_dir(),"
        "}))"
    )
    finished = subprocess.run(
        [str(environment.python), "-c", program],
        capture_output=True,
        text=True,
        check=True,
        cwd=elsewhere,
        timeout=300,
    )
    resolved = json.loads(finished.stdout)

    assert resolved["exists"]
    assert str(environment.root) in resolved["purge"]


# --- core's own Ansible travels the same way a capability's does ------------


def test_the_root_wheel_carries_the_platform_ansible_tree(wheels: dict[str, Path]):
    """Core keeps the contract it holds every capability to.

    `test_the_capability_wheel_carries_its_whole_ansible_tree` asserts this of
    `blitzecdn-cache`, and the reason given there is that a wheel shipping only
    the `.py` files leaves a controller pointing at a directory that is not
    there. Core is the larger case of exactly that: without this, `pip install
    blitzecdn` produces a control plane that cannot converge anything, and the
    only reason it works today is that `install.sh` and the Dockerfile copy the
    repository in behind it. That makes the checkout an undeclared runtime
    dependency of the root distribution — invisible until someone installs the
    wheel the way its own packaging says they may.
    """
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn"]) as archive:
        names = set(archive.namelist())

    root = "blitzecdn/ansible"
    for role in PLATFORM_ROLES:
        assert f"{root}/roles/{role}/tasks/main.yml" in names
        assert f"{root}/roles/{role}/meta/argument_specs.yml" in names
    for playbook in PLATFORM_PLAYBOOKS:
        assert f"{root}/playbooks/{playbook}" in names
    # The dynamic inventory plugin and the source file that selects it. The
    # fleet lives in the control-plane database and this is how Ansible reaches
    # it; a wheel without them has roles and no hosts.
    assert f"{root}/plugins/inventory/blitzecdn.py" in names
    assert f"{root}/inventory/blitzecdn.yml" in names
    # Shipped non-secret defaults. Read-only package data once they are in the
    # wheel, which is what turns CLAUDE.md's "do not edit the tracked files
    # under group_vars" from documentation into packaging.
    assert f"{root}/inventory/group_vars/blitzecdn_edges/defaults.yml" in names
    assert f"{root}/ansible.cfg" in names
    assert f"{root}/requirements.yml" in names


def test_the_root_wheel_carries_the_image_build_inputs(
    wheels: dict[str, Path],
):
    """The Dockerfiles ship too, for the same reason the roles do.

    They were a top-level `docker/` directory that only a checkout has, and
    every consumer — the justfile, both integration scripts, the release
    workflow, the contract suites and the compose template the controlplane
    role renders — spelled that path again. An air-gapped fleet that has to
    build its own edge image on the controller had nothing to build from, and
    a wheel is the only artefact that reaches such a controller.
    """
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn"]) as archive:
        names = set(archive.namelist())

    root = "blitzecdn/docker"
    assert f"{root}/edge/Dockerfile" in names
    # Not only the Dockerfile: the file it COPYs and probes with is part of
    # the context, and a build that shipped one without the other fails at
    # `nginx -t` in a layer nobody reads until the image is being published.
    assert f"{root}/edge/module-probe.conf" in names
    assert f"{root}/control-plane/Dockerfile" in names
    # BuildKit resolves this beside the Dockerfile, not at the context root, so
    # it travels in the wheel with it or the build context stops being pruned.
    assert f"{root}/control-plane/Dockerfile.dockerignore" in names


def test_core_locates_its_image_build_inputs_without_the_repository(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
):
    """`blitzecdn.docker` answers from site-packages, like `blitzecdn.ansible`.

    Shipping the files is half of it; resolving them without counting `..`
    from `__file__` is the other half, and it is the half that fails silently
    in a checkout where the two answers coincide. Run from a working directory
    that is not the repository, and asserted to be under the virtualenv.
    """
    environment = _environment(tmp_path_factory.mktemp("core-docker") / "venv")
    environment.install(wheels["blitzecdn"])
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    program = (
        "import json;"
        "from blitzecdn import docker;"
        "print(json.dumps({"
        "'context': str(docker.EDGE_CONTEXT),"
        "'exists': docker.EDGE_DOCKERFILE.is_file()"
        " and docker.EDGE_MODULE_PROBE_CONF.is_file()"
        " and docker.CONTROL_PLANE_DOCKERFILE.is_file()"
        " and docker.CONTROL_PLANE_DOCKERIGNORE.is_file(),"
        "}))"
    )
    finished = subprocess.run(
        [str(environment.python), "-c", program],
        capture_output=True,
        text=True,
        check=True,
        cwd=elsewhere,
        timeout=300,
    )
    resolved = json.loads(finished.stdout)

    assert resolved["exists"]
    assert str(environment.root) in resolved["context"]
    assert str(REPO_ROOT) not in resolved["context"]


def test_the_root_wheel_publishes_no_control_plane_build_context(
    wheels: dict[str, Path],
):
    """`blitzecdn.docker` names that Dockerfile and stops there.

    Its build context is the distribution's own source — `pyproject.toml`,
    `uv.lock` and every workspace member under `packages/` — so a constant for
    it would be `project_dir` under another name, put back by the very module
    that removed the last one. The role supplies the context, and only until
    the control plane is delivered as a published image the way the edge is.
    """
    import ast
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn"]) as archive:
        source = archive.read("blitzecdn/docker/__init__.py").decode("utf-8")

    module = ast.parse(source)
    # What is *defined*, not what is mentioned: the docstrings here explain the
    # absence, and a grep would forbid the explanation along with the thing.
    defined = {
        target.id
        for node in module.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    published = next(
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in node.targets
        )
    )

    assert "CONTROL_PLANE_CONTEXT" not in defined
    assert "CONTROL_PLANE_CONTEXT" not in published
    assert not any("CONTEXT" in name for name in published if "EDGE" not in name)


def test_core_locates_its_own_ansible_without_the_repository(
    tmp_path_factory: pytest.TempPathFactory, wheels: dict[str, Path]
):
    """The installed root wheel finds its roles and plays with no checkout.

    The mirror of `test_an_installed_capability_locates_its_plays_without_the
    _repository`, and it fails for the same reason that one would if
    `blitzecdn_cache.ansible` counted `..` from `__file__`: core resolves its
    tree from `Settings.project_dir`, so the answer is a repository-relative
    path that is correct in a checkout and absent on a controller.

    Run from a working directory that is not the repository, and asserted to be
    under the virtualenv, so neither `cwd` nor a stray `BLITZE_PROJECT_DIR` can
    make it pass for the wrong reason.
    """
    environment = _environment(tmp_path_factory.mktemp("core-ansible") / "venv")
    environment.install(wheels["blitzecdn"])
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    program = (
        "import json;"
        "from blitzecdn import ansible;"
        "print(json.dumps({"
        "'roles': str(ansible.ROLES_PATH),"
        "'edge': str(ansible.EDGE_PLAYBOOK),"
        "'names': sorted(p.name for p in ansible.ROLES_PATH.iterdir()"
        "  if p.is_dir()),"
        "'exists': ansible.EDGE_PLAYBOOK.is_file()"
        " and ansible.DECOMMISSION_PLAYBOOK.is_file()"
        " and ansible.ROLES_PATH.is_dir()"
        " and (ansible.INVENTORY_PLUGINS_PATH / 'blitzecdn.py').is_file(),"
        "}))"
    )
    finished = subprocess.run(
        [str(environment.python), "-c", program],
        capture_output=True,
        text=True,
        check=True,
        cwd=elsewhere,
        timeout=300,
    )
    resolved = json.loads(finished.stdout)

    assert resolved["exists"]
    assert sorted(PLATFORM_ROLES) == resolved["names"]
    assert str(environment.root) in resolved["roles"]
    assert str(environment.root) in resolved["edge"]
    assert str(REPO_ROOT) not in resolved["roles"]


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
        "cache_enabled": False,
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
    plain = core_only.site_capabilities({"compression": "off", "cache_enabled": False})
    filtered = core_only.site_capabilities(
        {
            "compression": "off",
            "cache_enabled": False,
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


def test_the_geoip_wheel_carries_its_own_edge_implementation(wheels: dict[str, Path]):
    """No vendored core — and the role that provisions the database, in full.

    Built, not assumed. A wheel that shipped only the `.py` files would leave
    the plugin contributing a directory that does not exist on an installed
    controller, and every deploy would fail with "the role was not found" — on
    the controller, never in this checkout.
    """
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn-geoip"]) as archive:
        names = set(archive.namelist())
        metadata = next(name for name in names if name.endswith("METADATA"))
        requires = archive.read(metadata).decode()

    assert sorted(name for name in names if name.endswith(".py")) == [
        "blitzecdn_geoip/__init__.py",
        "blitzecdn_geoip/ansible/__init__.py",
        "blitzecdn_geoip/plugin.py",
    ]
    root = "blitzecdn_geoip/ansible/roles/blitzecdn_geoip"
    assert f"{root}/tasks/main.yml" in names
    assert f"{root}/defaults/main.yml" in names
    assert f"{root}/meta/argument_specs.yml" in names
    # The updater's own Compose project and the systemd units that refresh the
    # database. A build that
    # filtered by extension would drop every one of them silently.
    for template in (
        "compose.yml.j2",
        "geoipupdate.env.j2",
        "geoipupdate.service.j2",
        "geoipupdate.timer.j2",
    ):
        assert f"{root}/templates/{template}" in names
    assert "blitzecdn_geoip/nginx/geoip-http.conf.j2" in names
    assert "blitzecdn_geoip/nginx/geoip-upstream.conf.j2" in names
    assert "Requires-Dist: blitzecdn" in requires


def test_the_security_wheel_carries_its_own_edge_implementation(
    wheels: dict[str, Path],
):
    """The njs challenge travels with the capability that declares it."""
    import zipfile

    with zipfile.ZipFile(wheels["blitzecdn-security"]) as archive:
        names = set(archive.namelist())

    root = "blitzecdn_security/ansible/roles/blitzecdn_security"
    assert f"{root}/tasks/main.yml" in names
    assert f"{root}/templates/under-attack.js.j2" in names
    assert "blitzecdn_security/nginx/security-http.conf.j2" in names
