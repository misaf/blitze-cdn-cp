"""Loaders and renderers shared by the edge-contract modules.

The role's own defaults, the Nginx resources the installed capabilities
contribute, the Jinja environment they are rendered in, and the helpers that
build a site and read a rendered block back. Helpers used by one module stay
beside its tests.
"""

from __future__ import annotations

import re
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
import yaml

# The nginx role renders from its own defaults *and* the shared edge runtime
# contract, so the loader that builds that namespace is shared with the other
# contract-test modules rather than reimplemented here.
from contract_support import (
    _role_defaults,
    _runtime_defaults,
    _split_runtime,
    ansible_bool,
)
from paths import CORE_ANSIBLE

from blitzecdn.capabilities.dns.domain import (
    CdnSite,
    DnsRecord,
    Domain,
    Rule,
)
from blitzecdn.capabilities.dns.domain.hosts import host_name
from blitzecdn.capabilities.tls.policy import (
    SslMode,
)
from blitzecdn.composition import load_control_plane_plugins
from blitzecdn.core.exceptions import ConflictError
from blitzecdn.core.plugins.resolution import resolve_nginx_resources

jinja2 = pytest.importorskip("jinja2")


#: The roles ship with this control plane, so there is no install step to get
#: wrong and no reason for these tests to skip. That matters: they used to read
#: an installed collection and skipped silently when it was absent, which turned
#: a broken contract into a green run.
ROLES_DIR = CORE_ANSIBLE / "roles"


def _role(name: str) -> Path:
    candidate = ROLES_DIR / name
    assert candidate.is_dir(), f"{name} is missing from src/blitzecdn/ansible/roles/"
    return candidate


ROLE_DIR = _role("blitzecdn_nginx")


def _nginx_resources() -> dict[str, list[dict[str, str]]]:
    return {
        context: [
            {
                "plugin": resource.plugin,
                "name": resource.name,
                "template": str(resource.template),
            }
            for resource in resources
        ]
        for context, resources in resolve_nginx_resources(
            load_control_plane_plugins().nginx_contributions()
        ).items()
    }


def _nginx_environment():
    environment = jinja2.Environment(
        loader=jinja2.FileSystemLoader(ROLE_DIR / "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    environment.filters["bool"] = ansible_bool

    @jinja2.pass_context
    def lookup(context, _plugin, template, *, template_vars=None):
        values = dict(context.get_all())
        values.update(template_vars or {})
        return environment.from_string(Path(template).read_text()).render(**values)

    environment.globals["lookup"] = lookup
    return environment


def _role_spec() -> dict[str, Any]:
    document = yaml.safe_load(
        (ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )
    return document["argument_specs"]["main"]["options"]


def _contract(*path: str) -> Any:
    """One member of the resolved edge runtime contract."""
    value: Any = _runtime_defaults()["blitzecdn_edge_runtime"]
    for key in path:
        value = value[key]
    return value


def _defaults_of(role_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((role_dir / "defaults/main.yml").read_text(encoding="utf-8"))


def _capability_defaults() -> dict[str, Any]:
    """Every installed capability role's defaults, the way a play resolves them.

    A capability's fragment reads its own role's variables — the cache zone,
    the compression level — and those are `defaults/main.yml` in a role that
    ships inside the wheel. Discovered through the contributions rather than
    listed by path: a checkout directory is not where an installed controller
    finds them, and naming the packages here would make core's tests need an
    edit every time a capability is attached or detached.
    """
    defaults: dict[str, Any] = {}
    for contribution in load_control_plane_plugins().ansible_contributions():
        for role in sorted(contribution.roles_path.iterdir()):
            if (role / "defaults/main.yml").is_file():
                defaults |= _defaults_of(role)
    return defaults


def _seed_site(repository, *, name, label, origin, **policy):
    """A zone carrying the policy, and one proxied hostname in it.

    Written through the stores rather than the services because these fixtures
    describe *state*, not the operations that produce it, and the desired-state
    document is what is under test.

    ``name`` is the host the caller expects to be derived, and it decides where
    the policy goes: ``example-com`` is the zone's own policy, and any other
    name becomes a rule matching this hostname. There is no site to create
    under either name. The origin is the record's value.
    """
    zone = "example.com"
    with suppress(ConflictError):
        repository.zones.create_domain(Domain(name=zone))
    if name == host_name(zone, None):
        current = repository.zones.get_domain(zone)
        repository.zones.replace_domain(
            Domain.model_validate({**current.model_dump(), **policy})
        )
    else:
        repository.rules.create_rule(
            Rule(
                domain=zone,
                name=name.removeprefix(f"{host_name(zone, None)}--"),
                match=f"{label}.{zone}",
                overrides=dict(policy),
            )
        )
    repository.zones.create_record(DnsRecord(domain=zone, name=label, value=origin))


def _render(site: dict[str, Any], **overrides: Any) -> str:
    environment = _nginx_environment()
    # A contract input has to be applied before the contract is composed, so
    # `blitzecdn_edge_geoip_enabled=True` reaches the template as
    # `blitzecdn_edge_runtime.geoip.enabled`, not as a stray extra variable.
    inputs, plain = _split_runtime(overrides)
    return environment.get_template("site.conf.j2").render(
        **(
            _role_defaults(**inputs)
            | _capability_defaults()
            | {"blitzecdn_nginx_resources": _nginx_resources()}
            | plain
        ),
        item=site,
    )


def _server_blocks(
    rendered: str, defaults: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Split a rendered site into its HTTP and HTTPS ``server`` blocks.

    Directives have to be attributed to the listener they belong to: "somewhere
    in the file" is exactly the assertion that let an origin TLS directive sit
    in a plaintext location unnoticed.
    """
    runtime = defaults["blitzecdn_edge_runtime"]
    blocks = ["server {" + part for part in rendered.split("server {")[1:]]
    http = [
        block
        for block in blocks
        if any(f"listen {port};" in block for port in runtime["listeners"]["http"])
    ]
    https = [
        block
        for block in blocks
        if any(f"listen {port} ssl;" in block for port in runtime["listeners"]["https"])
    ]
    assert len(http) + len(https) == len(blocks), "a server block matched neither set"
    return http, https


def _mode_site(mode: SslMode, serves_tls: bool, **extra: Any) -> CdnSite:
    payload: dict[str, Any] = {
        "name": "mode",
        "server_names": ["mode.example.com"],
        "origin_host": "origin.example.com",
        "ssl_mode": mode,
    }
    if serves_tls:
        payload |= {
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    return CdnSite.model_validate(payload | extra)


def _upstreams(rendered: str) -> set[str]:
    return set(re.findall(r"set \$blitzecdn_upstream (\S+);", rendered))
