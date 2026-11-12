"""Render the release the HTTP/3 harness asks its edge to serve.

The harness is `tests/integration/http3-edge-integration.sh`.

The edge play stages a configuration the controller rendered: it takes a
content-addressed `blitzecdn_release_digest` and the bundle rendered for it,
and it renders nothing itself. The harness has no database to compile a release
from, so this stands in for the compile step and nothing else. The desired
state is a committed YAML document instead of zones and rules; everything after
that is the control plane's own code — the installed features' contributions,
resolved by the functions the composition root calls, and `EdgeRenderer`, the
renderer `ReleaseService` uses — and its output has the shape
`deployments.wiring.publish_release` writes: the bundle, and an execution
document naming the digest and the bundle beside the desired state.

The digest is the SHA-256 of the canonical document, overrides included. A real
release's digest also covers the compiler version and the inputs, but the
property the edge depends on is the same: different desired state, different
candidate tree. That is why the harness puts `blitzecdn_edge_image` in the
document. A sealed candidate records the image it was validated under and reuses
it, so an image change under the same digest would never reach the container.

Path rules are the exception to a literal document. A location's pattern and
variable scope are derived from its rule, and the point of serving redirects is
to prove the locations production renders, so `--zones` names a document of
zones, rules and records instead. They are compiled into sites by the sites
capability's own `derive_hosts` and `site_to_ansible`, as `ReleaseService`
compiles a database's, and appended to `blitzecdn_nginx_sites`.

Run on the outer host, where the workspace is installed:

    uv run python tests/support/http3_release.py \
        STATE RUNNER OUTPUT EDGE_OUTPUT [--zones ZONES] [NAME=VAL...]

OUTPUT is written from scratch. EDGE_OUTPUT is the same directory as the edge
host sees it, because the execution document names the bundle by the path the
edge play's `copy` reads. Each NAME=VALUE replaces one top-level variable, with
VALUE parsed as YAML. Prints the digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from blitzecdn.assets import ansible as core_ansible
from blitzecdn.capabilities.sites.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.sites.domain import DnsRecord, Domain, Rule, derive_hosts
from blitzecdn.composition import BUILTIN_REGISTRY
from blitzecdn.core.ansible.contributions import EdgeContributions
from blitzecdn.core.ansible.rendering import EdgeRenderer
from blitzecdn.core.ansible.variables import read_variables
from blitzecdn.core.features import (
    resolve_edge_modules,
    resolve_nginx_resources,
    resolve_role_search_path,
)

#: What the edge play reads, by name, in OUTPUT.
EXECUTION_DOCUMENT = "release.yml"
CONTRIBUTIONS = "contributions.yml"
BUNDLE = "configuration-bundle.json"


def contributions(edge_roles: Sequence[str]) -> EdgeContributions:
    """The installed features' contributions, with the harness's edge roles.

    Resolved as `composition.infrastructure` resolves them, except for the edge
    slot: the harness converges a chosen subset of the capability roles (not
    `blitzecdn_resolver`, which would rewrite the DNS of a host whose resolver is
    Docker's), and the renderer has to load the defaults of exactly the roles the
    play runs.
    """
    features = BUILTIN_REGISTRY
    ansible = features.ansible_contributions()
    return EdgeContributions.of(
        roles_path=resolve_role_search_path(core_ansible.ROLES_PATH, ansible),
        edge_roles=edge_roles,
        nginx_resources=resolve_nginx_resources(features.nginx_contributions()),
        edge_modules=resolve_edge_modules(ansible),
    )


def compile_zones(path: Path) -> list[dict[str, Any]]:
    """The sites a zones document compiles to, by the control plane's own code.

    The document names one origin address and a list of zones, each with a
    `policy`, its `rules` and one `www` record pointing at that origin.
    """
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    domains: list[Domain] = []
    rules: list[Rule] = []
    records: list[DnsRecord] = []
    for zone in document["zones"]:
        name = zone["name"]
        defaults = Domain.model_validate({"name": name, "policy": {}})
        domains.append(
            Domain(name=name, policy=defaults.policy.applying(zone.get("policy", {})))
        )
        rules.extend(
            Rule.model_validate({"domain": name, **rule}) for rule in zone["rules"]
        )
        records.append(DnsRecord(domain=name, name="www", value=document["origin"]))
    return [site_to_ansible(host) for host in derive_hosts(domains, rules, records)]


def digest(document: dict[str, Any]) -> str:
    """The document's identity, independent of key order."""
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _override(argument: str) -> tuple[str, Any]:
    name, separator, value = argument.partition("=")
    if not separator or not name:
        raise argparse.ArgumentTypeError(f"{argument!r} is not NAME=VALUE")
    return name, yaml.safe_load(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("state", type=Path, help="The desired-state document.")
    parser.add_argument(
        "runner", type=Path, help="The runner's variables; names the edge roles."
    )
    parser.add_argument("output", type=Path)
    parser.add_argument("edge_output")
    parser.add_argument(
        "--zones",
        type=Path,
        help="Zones, rules and records compiled into further sites.",
    )
    parser.add_argument("overrides", nargs="*", type=_override)
    arguments = parser.parse_args(argv)

    document: dict[str, Any] = read_variables(arguments.state)
    document.update(dict(arguments.overrides))
    if arguments.zones is not None:
        document["blitzecdn_nginx_sites"] = [
            *document.get("blitzecdn_nginx_sites", []),
            *compile_zones(arguments.zones),
        ]
    runner: dict[str, Any] = read_variables(arguments.runner)
    contributed = contributions(runner["blitzecdn_capability_roles"])

    release = digest(document)
    execution = {**document, "blitzecdn_release_digest": release}
    bundle = EdgeRenderer(
        ansible_dir=core_ansible.ROLES_PATH.parent, contributions=contributed
    ).render(execution)

    shutil.rmtree(arguments.output, ignore_errors=True)
    arguments.output.mkdir(parents=True)
    (arguments.output / BUNDLE).write_text(bundle.encode(), encoding="utf-8")
    (arguments.output / EXECUTION_DOCUMENT).write_text(
        yaml.safe_dump(
            {
                **execution,
                "blitzecdn_config_release_bundle": f"{arguments.edge_output}/{BUNDLE}",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    # The module list the edge's invariant checks against. The resources are
    # rendered already; the edge only needs to know which modules they load.
    modules = contributed.variables()["blitzecdn_nginx_modules"]
    (arguments.output / CONTRIBUTIONS).write_text(
        yaml.safe_dump({"blitzecdn_nginx_modules": modules}, sort_keys=True),
        encoding="utf-8",
    )
    print(release)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
