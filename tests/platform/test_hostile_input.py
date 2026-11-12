"""Hostile operator text, through every free-text field the API accepts.

Each string a request body can carry is set, one at a time, to a small corpus
of values that mean something to a layer downstream: Jinja (`{{`), nginx (`;`),
YAML and log lines (newline), printf (`%x`), command lines (a leading `-`) and
globs (`*`). Each is sent over HTTP, so a patch is judged merged into the
resource it changes, and must either be refused or be stored exactly as sent.
The same corpus must then pass through the control plane's extra-vars writer
into a real `ansible-playbook` run unchanged.

The fields come from the published OpenAPI document rather than a list kept
here, so a field added later is exercised without anyone remembering to.
"""

from __future__ import annotations

import copy
import itertools
import json
import os
import shutil
import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr
from support.control_plane import (
    API_HEADERS,
    configured_settings,
    control_plane_app,
    seed_site_over_http,
)
from support.paths import REPO_ROOT

from blitzecdn.core.ansible.variables import run_variables
from blitzecdn.core.config import Settings

#: A Jinja expression, an nginx statement end, a line break, a printf
#: conversion, an option and a glob.
TOKENS = ("{{ 7*7 }}", "a;b", "a\nb", "%x", "-x", "*")

#: Each token alone, as a path and as a hostname label, so a field that wants a
#: leading `/` or a domain is offered something it might let through.
SPELLINGS = tuple(
    dict.fromkeys(
        spelling
        for token in TOKENS
        for spelling in (token, f"/{token}", f"{token}.example.com")
    )
)

#: Ordinary values, one of which each field must accept. Without this a field
#: whose body the walk builds wrongly is refused everything and passes.
BENIGN = (
    "a",
    "/a",
    "/a/",
    "example.com",
    "cdn.example.com",
    "198.51.100.20",
    "203.0.113.0/24",
    "edge-1",
    "deploy",
    "admin@example.com",
    "/home/deploy/.ssh/id_ed25519",
    ".css",
    "1d",
    "10m",
    "<p>x</p>",
    "example-com",
    "/etc/blitzecdn/tls/edge.pem",
    "503",
    "1r/s",
    "US",
    "GET",
)

#: The smallest valid body of each request, given a number that keeps the
#: names a creation needs unused. A request schema missing here fails
#: `test_every_request_body_has_a_baseline`.
BASELINES: dict[str, Callable[[int], dict[str, Any]]] = {
    "DomainCreate": lambda n: {"name": f"zone-{n}.example.org"},
    "DomainPatch": lambda n: {},
    "RecordCreate": lambda n: {"name": f"r{n}", "value": "198.51.100.20"},
    "RecordPatch": lambda n: {},
    # A rule that overrides nothing is refused: it would shadow every later one.
    "RuleCreate": lambda n: {
        "name": f"rule-{n}",
        "match": f"rule-{n}.example.com",
        "overrides": {"cache_enabled": False},
    },
    "RulePatch": lambda n: {},
    "EdgeCreate": lambda n: {"name": f"edge-{n}", "host": f"edge-{n}.example.com"},
    "EdgePatch": lambda n: {},
    "DeployRequest": lambda n: {},
    "RollbackRequest": lambda n: {},
    "RenewRequest": lambda n: {},
    "CertificateRequest": lambda n: {},
    # A purge must name something to purge.
    "PurgeRequest": lambda n: {"tags": ["blitze"]},
    "ConnectionsRequest": lambda n: {},
    "OriginCheckRequest": lambda n: {},
}

#: Request bodies that are not JSON. The certificate upload is two PEM files,
#: parsed as X.509 before anything reads them as text.
NOT_JSON = {"Body_upload_certificate_v3_sites__name__certificate_put"}

#: What the seeded state names each path parameter.
PARAMETERS = {
    "/v3/domains/{domain}": {"domain": "example.com"},
    "/v3/domains/{domain}/records": {"domain": "example.com"},
    "/v3/domains/{domain}/rules": {"domain": "example.com"},
    "/v3/domains/{domain}/records/{type}/{name}": {
        "domain": "example.com",
        "type": "A",
        "name": "cdn",
    },
    "/v3/domains/{domain}/rules/{name}": {"domain": "example.com", "name": "rule"},
    "/v3/edges/{name}": {"name": "edge-1"},
    "/v3/sites/{name}/certificate/request": {"name": "example-com"},
}

#: Required members of nested objects, filled in when a path passes through one.
NESTED: dict[str, dict[str, Any]] = {
    # The seeded site serves plain HTTP, and a purge of the other scheme is refused.
    "PurgeEntry": {"host": "cdn.example.com", "uri": "/a", "scheme": "http"},
    "PurgePrefix": {"host": "cdn.example.com"},
    "PathRedirect": {"target": "/a"},
    # Paths, burst and status mean nothing without a rate.
    "SiteRateLimit": {"rate": "1r/s"},
}

#: The key used when the hostile value is a mapping's value, and the value used
#: when it is the key, for mappings that constrain them.
DICT_KEYS = {"error_pages": "503", "cache_ttl_overrides": ".css"}
DICT_VALUES = {"error_pages": "<p>x</p>", "cache_ttl_overrides": "1d"}

#: A path step: a property name, `[]` for a list item, `{}` for a mapping's
#: value or `{key}` for a mapping's key, and the schema of the object the step
#: enters, if it enters one.
Step = tuple[str, str | None]
FieldPath = tuple[Step, ...]


#: Fields only an existing object's identifier fills: what they refuse says
#: nothing about the text, so no ordinary value is required to pass.
IDENTIFIERS = {"RollbackRequest.deployment_id", "RenewRequest.sites.[]"}


def _in_context(schema: str, path: FieldPath, body: dict[str, Any]) -> dict[str, Any]:
    """The body with what the field at ``path`` is meaningless without."""
    names = [step for step, _ in path]
    if names[-1] in ("certificate_path", "certificate_key_path"):
        policy = body["overrides"] if names[0] == "overrides" else body
        policy.setdefault("certificate_mode", "existing")
        policy.setdefault("certificate_path", "/etc/blitzecdn/tls/edge.pem")
        policy.setdefault("certificate_key_path", "/etc/blitzecdn/tls/edge.key")
    if schema in ("RuleCreate", "RulePatch") and names[0] == "redirect":
        # A path rule redirects or overrides, never both.
        body["path"] = "/old/"
        body["overrides"] = {}
    return body


def _document() -> dict[str, Any]:
    """The published OpenAPI document.

    Needed at collection, before any fixture, to parametrise over. Building it
    reads none of these paths, so they need not exist.
    """
    root = Path("/nonexistent/blitzecdn")
    settings = Settings(
        project_dir=root,
        state_dir=root / "state",
        database_path=root / "state/control-plane.db",
        ansible_dir=root / "ansible",
        inventory_path=root / "ansible/inventory/hosts.yml",
        playbook_path=root / "ansible/playbooks/edge.yml",
        generated_vars_path=root / "state/desired-state.yml",
        environment_path=root / ".env",
        ansible_playbook="/usr/bin/true",
        api_keys={"tester": SecretStr("x" * 32)},
    )
    return control_plane_app(settings).openapi()


def _operations(document: dict[str, Any]) -> dict[tuple[str, str], str]:
    """Each operation's request body schema, by method and path template."""
    found = {}
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            if body := operation.get("requestBody"):
                (content,) = body["content"].values()
                reference = content["schema"]["$ref"]
                found[method.upper(), path] = reference.rsplit("/", 1)[-1]
    return found


def _ref(schema: dict[str, Any]) -> str | None:
    reference = schema.get("$ref")
    return reference.rsplit("/", 1)[-1] if reference else None


def _entered(schema: dict[str, Any]) -> str | None:
    """The object schema a property enters, through a nullable union if need be."""
    return _ref(schema) or next(
        (name for branch in schema.get("anyOf", ()) if (name := _ref(branch))), None
    )


def _leaves(
    schema: dict[str, Any], schemas: dict[str, Any], path: FieldPath = ()
) -> Iterator[FieldPath]:
    """Every place in a body a free-text string can go."""
    if (name := _ref(schema)) is not None:
        yield from _leaves(schemas[name], schemas, path)
        return
    for branch in schema.get("anyOf", ()):
        if branch.get("type") != "null":
            yield from _leaves(branch, schemas, path)
    kind = schema.get("type")
    if kind == "string" and "enum" not in schema and "const" not in schema:
        yield path
    elif kind == "array":
        items = schema.get("items", {})
        yield from _leaves(items, schemas, (*path, ("[]", _ref(items))))
    elif kind == "object" or "properties" in schema:
        for member, child in schema.get("properties", {}).items():
            yield from _leaves(child, schemas, (*path, (member, _entered(child))))
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            yield (*path, ("{key}", None))
            yield from _leaves(extra, schemas, (*path, ("{}", _ref(extra))))


def _owner(path: FieldPath, index: int) -> str:
    """The property a `[]`, `{}` or `{key}` step belongs to."""
    return next((step for step, _ in reversed(path[:index]) if step[0] not in "[{"), "")


def _place(baseline: dict[str, Any], path: FieldPath, value: str) -> dict[str, Any]:
    """The baseline with ``value`` at ``path``, creating what lies between."""
    body = copy.deepcopy(baseline)
    node: Any = body
    for index, (step, entered) in enumerate(path):
        last = index == len(path) - 1
        fresh = copy.deepcopy(NESTED.get(entered or "", {}))
        if step == "[]":
            node.append(value if last else fresh)
            node = node[-1]
        elif step == "{}":
            key = DICT_KEYS.get(_owner(path, index), "k")
            node[key] = value if last else fresh
            node = node[key]
        elif step == "{key}":
            node[value] = DICT_VALUES.get(_owner(path, index), "v")
        elif last:
            node[step] = value
        else:
            following = path[index + 1][0]
            empty = [] if following == "[]" else {} if following[0] == "{" else fresh
            node = node.setdefault(step, empty)
    return body


def _held(resource: Any, path: FieldPath, value: str) -> Any:
    """What a stored resource holds where ``value`` was sent."""
    node = resource
    for index, (step, _) in enumerate(path):
        if step == "[]":
            node = node[-1]
        elif step == "{}":
            node = node[DICT_KEYS.get(_owner(path, index), "k")]
        elif step == "{key}":
            return value if value in node else sorted(node)
        else:
            node = node[step]
    return node


def _label(path: FieldPath) -> str:
    return ".".join(step for step, _ in path)


DOCUMENT = _document()
SCHEMAS = DOCUMENT["components"]["schemas"]
OPERATIONS = _operations(DOCUMENT)
FIELDS = [
    (method, route, schema, path)
    for (method, route), schema in sorted(OPERATIONS.items())
    if schema not in NOT_JSON
    for path in dict.fromkeys(_leaves(SCHEMAS[schema], SCHEMAS))
]


class Api:
    """A control plane with one of everything a request can name.

    Seeding takes longer than a field's whole test, so it is done once and the
    database put back before each field from a copy taken just after.
    """

    def __init__(self, client: TestClient, database: Path) -> None:
        self.client = client
        self.numbers = itertools.count(2)
        seed_site_over_http(client)
        # What `PARAMETERS` names: rule `rule` and edge `edge-1`.
        for route, body in (
            ("/v3/domains/example.com/rules", BASELINES["RuleCreate"](0)),
            ("/v3/edges", BASELINES["EdgeCreate"](1)),
        ):
            name = PARAMETERS[f"{route.replace('example.com', '{domain}')}/{{name}}"]
            response = client.post(
                route, json=body | {"name": name["name"]}, headers=API_HEADERS
            )
            assert response.is_success, response.text
        self.database = database
        self.seeded = sqlite3.connect(":memory:")
        with closing(sqlite3.connect(database)) as live:
            live.backup(self.seeded)

    def restore(self) -> None:
        with closing(sqlite3.connect(self.database)) as live:
            self.seeded.backup(live)

    def send(
        self, method: str, route: str, schema: str, path: FieldPath, value: str
    ) -> Any:
        """None when refused, else what the stored resource holds at ``path``.

        A queued operation answers with its job, not the resource, so what it
        accepted is reported as sent; the Ansible test carries it from there.
        """
        body = _place(BASELINES[schema](next(self.numbers)), path, value)
        body = _in_context(schema, path, body)
        response = self.client.request(
            method,
            route.format(**PARAMETERS.get(route, {})),
            json=body,
            headers=API_HEADERS,
        )
        if response.status_code >= 500:
            raise AssertionError(f"{response.status_code}: {response.text}")
        if not response.is_success:
            return None
        if response.status_code == 202:
            return value
        return _held(response.json(), path, value)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> Iterator[Api]:
    settings = configured_settings(tmp_path_factory.mktemp("hostile"))
    with TestClient(control_plane_app(settings)) as client:
        api = Api(client, settings.database_path)
        yield api
        api.seeded.close()


@pytest.fixture
def api(seeded) -> Api:
    seeded.restore()
    return seeded


def test_every_request_body_has_a_baseline():
    assert {schema for schema in OPERATIONS.values() if schema not in NOT_JSON} == set(
        BASELINES
    )
    assert {
        route
        for (_, route), schema in OPERATIONS.items()
        if "{" in route and schema not in NOT_JSON
    } == set(PARAMETERS)


def test_the_walk_reaches_the_fields_that_reach_an_edge():
    """A guard on the walk itself: a field it never finds is never tested."""
    labels = {f"{schema}.{_label(path)}" for _, _, schema, path in FIELDS}
    assert {
        "PurgeRequest.entries.[].uri",
        "PurgeRequest.prefixes.[].prefix",
        "PurgeRequest.tags.[]",
        "DomainPatch.maintenance_page",
        "DomainPatch.error_pages.{}",
        "DomainPatch.firewall.allow_sources.[]",
        "RuleCreate.overrides.firewall.denied_paths.[]",
        "RuleCreate.redirect.target",
        "RecordCreate.value",
        "EdgeCreate.host",
        "EdgePatch.host",
    } <= labels


@pytest.mark.parametrize(
    ("method", "route", "schema", "path"),
    FIELDS,
    ids=[f"{schema}.{_label(path)}" for _, _, schema, path in FIELDS],
)
def test_hostile_text_is_refused_or_stored_exactly(api, method, route, schema, path):
    if f"{schema}.{_label(path)}" not in IDENTIFIERS:
        assert any(
            api.send(method, route, schema, path, value) is not None for value in BENIGN
        ), "no ordinary value is accepted, so the body is built wrongly"
    altered = {
        value: held
        for value in SPELLINGS
        if (held := api.send(method, route, schema, path, value)) is not None
        and held != value
    }
    assert not altered, "accepted, but not as sent"


def _ansible_playbook() -> str:
    executable = shutil.which("ansible-playbook") or str(
        REPO_ROOT / ".venv/bin/ansible-playbook"
    )
    if not Path(executable).exists():
        pytest.skip("ansible-playbook is not installed")
    return executable


def test_hostile_text_reaches_a_play_literally(tmp_path):
    """Through the control plane's own extra-vars writer, into a real play.

    The play reads each value the way the roles do, through ``item`` in a loop
    over a list of objects, then writes down what it saw.
    """
    sent = [{"value": value} for value in SPELLINGS]
    seen = tmp_path / "seen.json"
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        yaml.safe_dump(
            [
                {
                    "hosts": "localhost",
                    "gather_facts": False,
                    "tasks": [
                        {
                            "ansible.builtin.set_fact": {
                                "blitzecdn_seen": "{{ (blitzecdn_seen | default([]))"
                                " + [{'value': item.value}] }}"
                            },
                            "loop": "{{ blitzecdn_hostile }}",
                        },
                        {
                            "ansible.builtin.copy": {
                                "content": "{{ blitzecdn_seen | to_json }}",
                                "dest": str(seen),
                                "mode": "0600",
                            }
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "ansible-local").mkdir()
    with run_variables(
        tmp_path / "vars", "hostile", {"blitzecdn_hostile": sent}
    ) as variables:
        result = subprocess.run(
            [
                _ansible_playbook(),
                "-i",
                "localhost,",
                "-c",
                "local",
                "--extra-vars",
                f"@{variables}",
                str(playbook),
            ],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=False,
            env=os.environ | {"ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-local")},
        )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(seen.read_text(encoding="utf-8")) == sent
