"""The throwaway environments the packaging lifecycle is asserted against.

``Environment`` is a virtualenv this suite built and can ask questions of;
``_environment`` builds one and installs into it. The session fixtures that own
the three standing environments are in ``conftest.py`` beside this, because a
fixture has to be registered with pytest rather than imported.
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
from typing import Any

from paths import REPO_ROOT

#: The `uv` this developer or this CI job is actually running, resolved once
#: rather than spelled as a bare name on every call. A partial path would be
#: whatever `PATH` happened to hold when a subprocess started, and these
#: subprocesses build and install wheels.


#: The `uv` this developer or this CI job is actually running, resolved once
#: rather than spelled as a bare name on every call. A partial path would be
#: whatever `PATH` happened to hold when a subprocess started, and these
#: subprocesses build and install wheels.
UV = shutil.which("uv")


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
            "certificate_path": "/etc/blitzecdn/tls/example-com/fullchain.pem",
            "certificate_key_path": "/etc/blitzecdn/tls/example-com/privkey.pem",
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
            "certificate_path": "/etc/ssl/example-com.pem",
            "certificate_key_path": "/etc/ssl/example-com.key",
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
            "from blitzecdn.composition import load_control_plane_plugins;"
            "r = load_control_plane_plugins();"
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

    def ansible_roles(self) -> dict[str, Any]:
        """Which role directories this installation would give Ansible.

        Resolved the way a deployment resolves it — the registry's
        contributions through `resolve_role_search_path` — and reported as
        paths and role names, so the assertion can be about what is installed
        rather than about what the source tree happens to contain.
        """
        program = (
            "import json;"
            "from pathlib import Path;"
            "from blitzecdn.composition import load_control_plane_plugins;"
            "from blitzecdn.core.plugins.resolution import ("
            "  resolve_edge_capability_roles, resolve_host_capability_roles,"
            "  resolve_role_search_path, resolve_teardown_capability_roles);"
            "from blitzecdn.core.plugins.resolution import ("
            "  resolve_edge_modules, resolve_nginx_resources);"
            # The platform's own roles, from the installed distribution. This
            # used to be a fabricated path, because core resolved its tree from
            # the checkout and there was nothing to point at in a virtualenv.
            "from blitzecdn.ansible import ROLES_PATH as core;"
            "path = resolve_role_search_path(core, load_control_plane_plugins()"
            ".ansible_contributions());"
            "print(json.dumps({"
            "'paths': [str(p) for p in path],"
            "'roles': sorted(r.name for p in path if p.is_dir()"
            "  for r in p.iterdir() if r.is_dir()),"
            # And which of those roles the edge play would run, resolved the
            # same way and from the same contributions. Two questions with one
            # source: a package may ship a role only its own plays reach.
            "'edge_roles': list(resolve_edge_capability_roles("
            "  load_control_plane_plugins().ansible_contributions())),"
            # And the play's other slot, which is a separate list because it is
            # a separate position in the play: what a capability does to the
            # host once the edge is already serving.
            "'host_roles': list(resolve_host_capability_roles("
            "  load_control_plane_plugins().ansible_contributions())),"
            # And the decommission play's slot, which is the one a capability
            # uses to take its own files off a host that is leaving.
            "'teardown_roles': list(resolve_teardown_capability_roles("
            "  load_control_plane_plugins().ansible_contributions())),"
            # And the Nginx dynamic modules those roles' configuration needs
            # loaded. The image is built from this list and the edge renders
            # its own from it, so a detached capability whose module still
            # appeared here would be an edge loading it forever.
            "'modules': [[m.plugin, m.name] for m in resolve_edge_modules("
            "  load_control_plane_plugins().ansible_contributions())],"
            "'nginx': {context:[{'plugin':r.plugin,'name':r.name,"
            "  'exists':r.template.is_file()} for r in resources]"
            "  for context,resources in resolve_nginx_resources("
            "    load_control_plane_plugins().nginx_contributions()).items()},"
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
            "from blitzecdn.composition import load_control_plane_plugins;"
            "from blitzecdn.capabilities.dns.domain import CdnSite;"
            "values={'name':'example-com',"
            "'server_names':['cdn.example.com'],"
            "'origin_host':'198.51.100.10',**json.loads(sys.argv[1])};"
            "site=CdnSite.model_validate(values);"
            "registry=load_control_plane_plugins();"
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
            "from blitzecdn.composition import load_control_plane_plugins;"
            "from blitzecdn.capabilities.dns.domain import CdnSite;"
            "sites=tuple(CdnSite.model_validate(v) for v in json.loads(sys.argv[1]));"
            "registry = load_control_plane_plugins();"
            "print(json.dumps(registry.fleet_variables(sites, object())))"
        )
        finished = subprocess.run(
            [str(self.python), "-c", program, json.dumps(sites)],
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
        return json.loads(finished.stdout)


def _environment(root: Path) -> Environment:
    _uv(
        "venv",
        "--python",
        f"{sys.version_info.major}.{sys.version_info.minor}",
        str(root),
    )
    return Environment(root)


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
    "blitzecdn_edge_teardown",
    "blitzecdn_uninstall",
)


#: The capability whose Ansible really is its own: two roles and two plays that
#: exist only while `blitzecdn-cache` is installed. `backup` drives the generic
#: attach/detach cycle above because it has no Ansible at all, which is exactly
#: why it cannot drive this one.
ANSIBLE_PACKAGE = "blitzecdn-cache"
