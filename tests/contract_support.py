"""Shared paths and loaders for Ansible contract tests.

Paths, readers and the one fixture, and nothing else. This module used to
re-export the domain types and the standard library alongside them so that
consumers could say ``from contract_support import *`` — which left a reader
unable to tell where ``SslMode`` came from, and left ruff unable to see that
``subprocess`` was the standard library, so S603 fired on none of the eight
``ansible-playbook`` calls the contract suites make. The consumers import what
they use from where it is defined now.
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
from paths import CORE_ANSIBLE, FIXTURES, REPO_ROOT

from blitzecdn.capabilities.dns.domain import DnsRecord, Domain, Rule
from blitzecdn.capabilities.dns.domain.hosts import host_name
from blitzecdn.capabilities.tls.policy import CertificateMode, SslMode
from blitzecdn.composition import ControlPlane, Repository
from blitzecdn.core.config import Settings
from blitzecdn.core.exceptions import ConflictError

jinja2 = pytest.importorskip("jinja2")

PROJECT_DIR = REPO_ROOT
FIXTURE = FIXTURES / "desired-state.yml"


#: Core's roles. They ship with this control plane, so there is no install step
#: to get wrong and no reason for these tests to skip. That matters: they used
#: to read an installed collection and skipped silently when it was absent,
#: which turned a broken contract into a green run.
#:
#: An optional capability's roles are not here. They ship inside that
#: capability's wheel and its own tests read them from there, which is the same
#: path a deployment resolves them by.
ROLES_DIR = CORE_ANSIBLE / "roles"


def _role(name: str) -> Path:
    candidate = ROLES_DIR / name
    assert candidate.is_dir(), f"{name} is missing from src/blitzecdn/ansible/roles/"
    return candidate


ROLE_DIR = _role("blitzecdn_nginx")

#: The role that owns the shared edge runtime contract. blitzecdn_nginx,
#: blitzecdn_edge_stack and blitzecdn_firewall all read `blitzecdn_edge_runtime`
#: from here and none of them reads the others, so every test that renders one
#: of those roles needs the contract resolved first.
RUNTIME_ROLE_DIR = _role("blitzecdn_edge")


def ansible_bool(value: Any) -> bool:
    """Ansible's `bool` filter, which is not Jinja's and not Python's.

    Python calls every non-empty string true, so `bool("false")` is True and a
    switch an operator passed as `-e name=false` comes out backwards. Ansible
    reads the word. Exported because the templates use the filter and the
    environments that render them here are plain Jinja: one of them defined
    `bool` as Python's, which is the very confusion the filter exists to end.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "on", "1"}


def _ansible_jinja(**kwargs: Any) -> Any:
    """A Jinja environment with the handful of Ansible filters the edge uses.

    Rendering the real templates and resolving the real defaults is the point:
    a compose file asserted on as text cannot tell a mount from a comment, and
    a contract asserted on before its expressions are evaluated is a dict of
    Jinja source.
    """
    environment = jinja2.Environment(undefined=jinja2.StrictUndefined, **kwargs)
    environment.filters["dirname"] = os.path.dirname
    environment.filters["basename"] = os.path.basename
    environment.filters["regex_replace"] = lambda value, pattern, replacement="": (
        re.sub(pattern, replacement, value)
    )
    environment.filters["bool"] = ansible_bool
    # `lookup('env', ...)` is Ansible's, not Jinja's. The defaults that use it
    # are secrets read from the controller's environment and are none of these
    # tests' business.
    environment.globals["lookup"] = lambda *_args, **_kwargs: ""
    return environment


def _resolve(value: Any, context: dict[str, Any], environment: Any) -> Any:
    """Render every Jinja expression nested anywhere inside ``value``.

    Ansible evaluates a default lazily, wherever it is used, so a contract
    member written as an expression is a real value by the time a role reads
    it. These tests have to do the same or they assert on template source.
    """
    if isinstance(value, str):
        if "{{" not in value:
            return value
        rendered = environment.from_string(value).render(**context).strip()
        return {"True": True, "False": False}.get(rendered, rendered)
    if isinstance(value, dict):
        return {
            key: _resolve(item, context, environment) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve(item, context, environment) for item in value]
    return value


def _yaml_mapping(path: Path) -> dict[str, Any]:
    """One YAML document, as the mapping every caller here expects.

    PyYAML is untyped, so `safe_load` is `Any` and every reader below used to
    hand that straight back through a `dict[str, Any]` annotation — which is
    how a `dict[str, Any]` becomes a promise nothing keeps. Asserting the shape
    once is what turns the annotation into a fact, and it fails on the file
    that is wrong rather than at whichever subscript first noticed.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path} is not a mapping"
    return document


def _runtime_source() -> dict[str, Any]:
    return _yaml_mapping(RUNTIME_ROLE_DIR / "defaults/main.yml")


#: The contract's flat inputs — the members that are not fixed runtime layout.
#: They are flat because desired state and `blitzecdn config set` reach Ansible
#: as top-level variables and neither can override one member of a dictionary.
RUNTIME_INPUTS = frozenset(_runtime_source()) - {"blitzecdn_edge_runtime"}


def _runtime_defaults(**inputs: Any) -> dict[str, Any]:
    """The contract as a role sees it: every expression already evaluated."""
    source = _runtime_source() | inputs
    environment = _ansible_jinja()
    return source | {
        "blitzecdn_edge_runtime": _resolve(
            source["blitzecdn_edge_runtime"], source, environment
        )
    }


def _split_runtime(overrides: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Separate contract inputs from ordinary variable overrides.

    A contract input has to be applied before the contract is composed —
    setting `blitzecdn_edge_http3_enabled` after the fact would leave
    `blitzecdn_edge_runtime.listeners.http3` reading the default, which is
    precisely the two-copies bug the contract exists to remove.
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


def _role_defaults(**runtime_inputs: Any) -> dict[str, Any]:
    """blitzecdn_nginx's defaults on top of the resolved runtime contract.

    Which is the variable namespace the role actually renders from: it declares
    `blitzecdn_edge_runtime` as a required option and reads the paths, the
    listener sets, the status endpoint and the GeoIP database from it.
    """
    context = _runtime_defaults(**runtime_inputs) | _yaml_mapping(
        ROLE_DIR / "defaults/main.yml"
    )
    # Resolved, because Ansible resolves: a default written as an expression
    # over the contract — the status file's path, the access log's — is a real
    # value by the time a template reads it, and a test comparing template
    # source against a literal proves nothing.
    environment = _ansible_jinja()
    for _ in range(len(context)):
        resolved = {
            name: value
            for name, value in context.items()
            if name != "blitzecdn_edge_runtime"
        }
        resolved = _resolve(resolved, context, environment)
        if resolved == {
            name: value
            for name, value in context.items()
            if name != "blitzecdn_edge_runtime"
        }:
            break
        context |= resolved
    return context


STACK_ROLE_DIR = _role("blitzecdn_edge_stack")
DOCKER_ROLE_DIR = _role("blitzecdn_docker")


def run_role_tasks(
    tasks_file: Path, variables: dict[str, Any], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    """Execute one role's task file against localhost and report what happened.

    The only way these assertions are ever *evaluated* rather than merely
    parsed. `--syntax-check` and ansible-lint both accept a `when:` that raises
    at run time, and a conditional whose result is a dict rather than a boolean
    is exactly the shape that has shipped a broken deploy before.

    Shared here because a capability's role validates its own settings now, in
    its own package's tests, and every one of them needs the same three lines
    of playbook around a task file.
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
            # A capability's role uses the same collections core's roles do —
            # `community.docker` runs its updater container. Without this the
            # play fails to resolve the module, which looks like a broken role
            # rather than a test harness that never installed anything.
            "ANSIBLE_COLLECTIONS_PATH": str(PROJECT_DIR / ".state/collections"),
        },
    )


def _seed_site(
    repository: Repository,
    *,
    name: str,
    label: str,
    origin: str,
    **policy: Any,
) -> None:
    """A zone carrying the policy, and one proxied hostname in it.

    Written through the stores rather than the services because these fixtures
    describe *state*, not the operations that produce it, and the desired-state
    document is what is under test.

    ``name`` is the host the caller expects to be derived. There is no site to
    create under that name any more, so it decides whether the policy goes on
    the zone or on a rule: ``example-com`` is the zone's own, and anything else
    becomes a rule matching this hostname. Either way the record's value is the
    origin the edge fetches from.
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


@pytest.fixture
def desired_state(settings: Settings, tmp_path: Path) -> dict[str, Any]:
    repository = Repository(settings.database_path)
    control = ControlPlane(settings=settings, repository=repository)
    repository.zones.create_domain(Domain(name="example.com"))
    _seed_site(
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
    _seed_site(
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
    control.deployments.write_desired_state(
        repository.snapshot(), settings.generated_vars_path
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
    "_ansible_jinja",
    "_resolve",
    "_role",
    "_role_defaults",
    "_role_spec",
    "_runtime_defaults",
    "_seed_site",
    "_split_runtime",
    "ansible_bool",
    "desired_state",
    "jinja2",
    "run_role_tasks",
]
