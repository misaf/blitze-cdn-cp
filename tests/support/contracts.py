"""Shared paths and loaders for Ansible contract tests.

Paths, readers and the one fixture, and nothing else.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
import yaml

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, Rule
from blitzecdn.capabilities.dns.domain.hosts import host_name
from blitzecdn.capabilities.tls.contract import CertificateMode, SslMode
from blitzecdn.composition import Repository, load_registry
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import ConflictError
from support.control_plane import build_application
from support.paths import CORE_ANSIBLE, FIXTURES, REPO_ROOT
from support.policy import domain_from_settings

jinja2 = pytest.importorskip("jinja2")

PROJECT_DIR = REPO_ROOT
FIXTURE = FIXTURES / "desired-state.yml"


#: Core's roles. They ship with this control plane, so these tests never skip.
#: Capability and provider roles are listed by `contributed_roles`.
ROLES_DIR = CORE_ANSIBLE / "roles"


def contributed_roles() -> dict[str, Path]:
    """Every role a built-in capability or provider contributes, by name.

    Read from the feature registry rather than a directory glob, so a test cannot
    quietly check fewer roles than a deployment runs.
    """
    features = load_registry()
    roles = {
        f"{contribution.feature}:{directory.name}": directory
        for contribution in features.ansible_contributions()
        for directory in sorted(contribution.roles_path.iterdir())
        if directory.is_dir()
    }
    assert roles, "no capability contributes an Ansible role"
    return roles


def role_path(name: str) -> Path:
    candidate = ROLES_DIR / name
    assert candidate.is_dir(), (
        f"{name} is missing from src/blitzecdn/assets/ansible/roles/"
    )
    return candidate


ROLE_DIR = role_path("blitzecdn_nginx")

#: The role that owns the shared edge runtime contract. Every role that reads
#: `blitzecdn_edge_runtime` needs it resolved first.
RUNTIME_ROLE_DIR = role_path("blitzecdn_edge")


def ansible_bool(value: Any) -> bool:
    """Ansible's `bool` filter, which is not Jinja's and not Python's.

    Python calls every non-empty string true, so `-e name=false` would come out
    backwards. Ansible reads the word.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "on", "1"}


def ansible_jinja(**kwargs: Any) -> Any:
    """A Jinja environment with the handful of Ansible filters the edge uses.

    The real templates and resolved defaults are rendered, since a contract
    asserted on before evaluation is a dict of Jinja source.
    """
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined, **kwargs)
    environment.filters["dirname"] = os.path.dirname
    environment.filters["basename"] = os.path.basename
    environment.filters["regex_replace"] = lambda value, pattern, replacement="": (
        re.sub(pattern, replacement, value)
    )
    environment.filters["bool"] = ansible_bool
    # `lookup('env', ...)` is Ansible's; the defaults using it are controller
    # secrets and none of these tests' business.
    environment.globals["lookup"] = lambda *_args, **_kwargs: ""
    return environment


def resolve(value: Any, context: dict[str, Any], environment: Any) -> Any:
    """Render every Jinja expression nested anywhere inside ``value``.

    Ansible evaluates defaults lazily, so a contract member written as an
    expression is a real value by the time a role reads it.
    """
    if isinstance(value, str):
        if "{{" not in value:
            return value
        rendered = environment.from_string(value).render(**context).strip()
        return {"True": True, "False": False}.get(rendered, rendered)
    if isinstance(value, dict):
        return {key: resolve(item, context, environment) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, context, environment) for item in value]
    return value


def _yaml_mapping(path: Path) -> dict[str, Any]:
    """One YAML document, as the mapping every caller here expects.

    `safe_load` is `Any`; asserting the shape once makes the annotation true and
    fails on the file that is wrong.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path} is not a mapping"
    return document


def _runtime_source() -> dict[str, Any]:
    return _yaml_mapping(RUNTIME_ROLE_DIR / "defaults/main.yml")


#: The contract's flat inputs. Desired state and `blitzecdn config set` reach
#: Ansible as top-level variables and cannot override one member of a dict.
RUNTIME_INPUTS = frozenset(_runtime_source()) - {"blitzecdn_edge_runtime"}


def runtime_defaults(**inputs: Any) -> dict[str, Any]:
    """The contract as a role sees it: every expression already evaluated."""
    source = _runtime_source() | inputs
    environment = ansible_jinja()
    return source | {
        "blitzecdn_edge_runtime": resolve(
            source["blitzecdn_edge_runtime"], source, environment
        )
    }


def split_runtime(overrides: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate contract inputs from ordinary variable overrides.

    A contract input must be applied before the contract is composed, or
    `blitzecdn_edge_runtime.listeners.http3` would still read the default.
    """
    inputs = {
        name: value for name, value in overrides.items() if name in RUNTIME_INPUTS
    }
    return inputs, {
        name: value for name, value in overrides.items() if name not in RUNTIME_INPUTS
    }


class _IndentedDumper(yaml.SafeDumper):
    """Indent sequences under their key, which is what yamllint expects."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        return super().increase_indent(flow, False)


def _role_spec() -> dict[str, Any]:
    document = _yaml_mapping(ROLE_DIR / "meta/argument_specs.yml")
    options = document["argument_specs"]["main"]["options"]
    assert isinstance(options, dict), "argument_specs main options is not a mapping"
    return options


def role_defaults(**runtime_inputs: Any) -> dict[str, Any]:
    """blitzecdn_nginx's defaults on top of the resolved runtime contract.

    That is the namespace the role renders from: it requires
    `blitzecdn_edge_runtime` and reads paths, listeners, the status endpoint and
    the GeoIP database from it.
    """
    context = runtime_defaults(**runtime_inputs) | _yaml_mapping(
        ROLE_DIR / "defaults/main.yml"
    )
    # Resolved, because Ansible resolves: a default written over the contract is a
    # real value by the time a template reads it.
    environment = ansible_jinja()
    for _ in range(len(context)):
        resolved = {
            name: value
            for name, value in context.items()
            if name != "blitzecdn_edge_runtime"
        }
        resolved = resolve(resolved, context, environment)
        if resolved == {
            name: value
            for name, value in context.items()
            if name != "blitzecdn_edge_runtime"
        }:
            break
        context |= resolved
    return context


STACK_ROLE_DIR = role_path("blitzecdn_edge_stack")
DOCKER_ROLE_DIR = role_path("blitzecdn_docker")


def run_role_tasks(
    tasks_file: Path, variables: dict[str, Any], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    """Execute one role's task file against localhost and report what happened.

    The only way these assertions are evaluated rather than parsed:
    `--syntax-check` and ansible-lint both accept a `when:` that raises at run
    time, or one that yields a dict instead of a boolean.
    """
    executable = shutil.which("ansible-playbook") or str(
        PROJECT_DIR / ".venv/bin/ansible-playbook"
    )
    if not Path(executable).exists():
        pytest.skip("ansible-playbook is not installed")
    (tmp_path / "ansible-local").mkdir(exist_ok=True)
    playbook = tmp_path / f"{tasks_file.stem}-run.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "vars": variables,
                    "tasks": [{"import_tasks": str(tasks_file)}],
                }
            ]
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [executable, "-i", "localhost,", "-c", "local", str(playbook)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
        env=os.environ
        | {
            "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-local"),
            "ANSIBLE_ROLES_PATH": str(ROLES_DIR),
            # Capability roles use the same collections as core's (`community.docker`
            # runs the updater container).
            "ANSIBLE_COLLECTIONS_PATH": str(PROJECT_DIR / ".state/collections"),
        },
    )


def seed_site(
    repository: Repository,
    *,
    name: str,
    label: str,
    origin: str,
    **policy: Any,
) -> None:
    """A zone carrying the policy, and one proxied hostname in it.

    Written through the stores: these fixtures describe state, and the
    desired-state document is what is under test.

    ``name`` is the host the caller expects: ``example-com`` puts the policy on the
    zone, anything else on a rule matching this hostname. The record's value is
    the origin.
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


@pytest.fixture
def desired_state(settings: Settings, tmp_path: Path) -> dict[str, Any]:
    repository = Repository(settings.database_path)
    control = build_application(settings=settings, repository=repository)
    repository.zones.create_domain(Domain(name="example.com"))
    seed_site(
        repository,
        name="example-com",
        label="cdn",
        origin="198.51.100.20",
        **{
            "ssl_mode": SslMode.OFF,
            "origin_request_host": "origin.example.com",
            "origin_sni": "origin.example.com",
            "cache_enabled": True,
            "cache_valid_success": "10m",
            "cache_valid_not_found": "1m",
        },
    )
    seed_site(
        repository,
        name="static-example-com",
        label="static",
        origin="192.0.2.10",
        **{
            "ssl_mode": SslMode.FLEXIBLE,
            "enabled": False,
            "cache_enabled": False,
            "certificate_mode": CertificateMode.EXISTING,
            "certificate_path": "/etc/ssl/plain/fullchain.pem",
            "certificate_key_path": "/etc/ssl/plain/privkey.pem",
            "firewall": {
                "allow_sources": ["203.0.113.9"],
                "deny_sources": ["203.0.113.0/24", "2001:db8::/32"],
                "denied_methods": ["DELETE", "TRACE"],
                "denied_paths": ["/admin", "/.git"],
            },
        },
    )
    control.deployments.publish_artifact(
        control.releases.compile(), settings.generated_vars_path
    )
    return _yaml_mapping(settings.generated_vars_path)


__all__ = [
    "DOCKER_ROLE_DIR",
    "FIXTURE",
    "PROJECT_DIR",
    "ROLES_DIR",
    "ROLE_DIR",
    "RUNTIME_INPUTS",
    "RUNTIME_ROLE_DIR",
    "STACK_ROLE_DIR",
    "_IndentedDumper",
    "_role_spec",
    "ansible_bool",
    "ansible_jinja",
    "desired_state",
    "jinja2",
    "resolve",
    "role_defaults",
    "role_path",
    "run_role_tasks",
    "runtime_defaults",
    "seed_site",
    "split_runtime",
]
