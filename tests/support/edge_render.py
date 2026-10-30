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
from typing import Any, cast

import jinja2
import jinja2.runtime
import yaml

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, Rule
from blitzecdn.capabilities.dns.domain.hosts import host_name
from blitzecdn.capabilities.tls.contract import (
    SslMode,
)
from blitzecdn.composition import Repository, load_registry
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.exceptions import ConflictError
from blitzecdn.core.features.resolution import resolve_nginx_resources

# The nginx role renders from its defaults and the shared edge runtime
# contract, so this loader is shared with the other contract-test modules.
from support.contracts import (
    ansible_bool,
    role_defaults,
    runtime_defaults,
    split_runtime,
)
from support.paths import CORE_ANSIBLE
from support.policy import domain_from_settings, site_from_settings

#: The roles ship with this control plane, so these tests never skip.
ROLES_DIR = CORE_ANSIBLE / "roles"


def role_path(name: str) -> Path:
    candidate = ROLES_DIR / name
    assert candidate.is_dir(), (
        f"{name} is missing from src/blitzecdn/assets/ansible/roles/"
    )
    return candidate


ROLE_DIR = role_path("blitzecdn_nginx")


def nginx_resources() -> dict[str, list[dict[str, str]]]:
    return {
        context: [
            {
                "feature": resource.feature,
                "name": resource.name,
                "template": str(resource.template),
            }
            for resource in resources
        ]
        for context, resources in resolve_nginx_resources(
            load_registry().nginx_contributions()
        ).items()
    }


def nginx_environment() -> jinja2.Environment:
    environment = jinja2.Environment(  # noqa: S701 - renders nginx, not HTML
        loader=jinja2.FileSystemLoader(ROLE_DIR / "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    environment.filters["bool"] = ansible_bool

    @jinja2.pass_context
    def lookup(
        context: jinja2.runtime.Context,
        _plugin: str,
        template: str,
        *,
        template_vars: dict[str, Any] | None = None,
    ) -> str:
        values = dict(context.get_all())
        values.update(template_vars or {})
        return environment.from_string(Path(template).read_text()).render(**values)

    environment.globals["lookup"] = lookup
    return environment


def role_spec() -> dict[str, Any]:
    document = yaml.safe_load(
        (ROLE_DIR / "meta/argument_specs.yml").read_text(encoding="utf-8")
    )
    return cast(dict[str, Any], document["argument_specs"]["main"]["options"])


def runtime_contract(*path: str) -> Any:
    """One member of the resolved edge runtime contract."""
    value: Any = runtime_defaults()["blitzecdn_edge_runtime"]
    for key in path:
        value = value[key]
    return value


def defaults_of(role_dir: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.safe_load((role_dir / "defaults/main.yml").read_text(encoding="utf-8")),
    )


def _capability_defaults() -> dict[str, Any]:
    """Every installed capability role's defaults, the way a play resolves them.

    Discovered through the contributions rather than by path, so attaching a
    capability needs no edit here.
    """
    defaults: dict[str, Any] = {}
    for contribution in load_registry().ansible_contributions():
        for role in sorted(contribution.roles_path.iterdir()):
            if (role / "defaults/main.yml").is_file():
                defaults |= defaults_of(role)
    return defaults


def seed_site(
    repository: Repository, *, name: str, label: str, origin: str, **policy: Any
) -> None:
    """A zone carrying the policy, and one proxied hostname in it.

    Written through the stores: these fixtures describe state, and the
    desired-state document is what is under test. ``example-com`` puts the policy
    on the zone, any other name on a rule matching this hostname. The origin is the
    record's value.
    """
    zone = "example.com"
    with suppress(ConflictError):
        repository.zones.create_domain(Domain(name=zone))
    if name == host_name(zone, None):
        current = repository.zones.get_domain(zone)
        repository.zones.replace_domain(
            domain_from_settings({**current.model_dump(), **policy})
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


def render_site(site: dict[str, Any], **overrides: Any) -> str:
    environment = nginx_environment()
    # A contract input is applied before the contract is composed, so
    # `blitzecdn_edge_geoip_enabled=True` reaches the template as
    # `blitzecdn_edge_runtime.geoip.enabled`.
    inputs, plain = split_runtime(overrides)
    return environment.get_template("site.conf.j2").render(
        **(
            role_defaults(**inputs)
            | _capability_defaults()
            | {"blitzecdn_nginx_resources": nginx_resources()}
            | plain
        ),
        item=site,
    )


def server_blocks(
    rendered: str, defaults: dict[str, Any]
) -> tuple[list[str], list[str]]:
    """Split a rendered site into its HTTP and HTTPS ``server`` blocks.

    Directives must be attributed to their listener: "somewhere in the file" let
    an origin TLS directive sit in a plaintext location unnoticed.
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


def mode_site(mode: SslMode, serves_tls: bool, **extra: Any) -> CdnSite:
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
    return site_from_settings(payload | extra)


def rendered_upstreams(rendered: str) -> set[str]:
    return set(re.findall(r"set \$blitzecdn_upstream (\S+);", rendered))
