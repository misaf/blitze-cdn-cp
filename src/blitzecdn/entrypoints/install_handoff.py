"""Post-bootstrap work invoked by ``install.sh`` through the installed runtime."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from blitzecdn.capabilities.deployments.domain import DeploymentStatus
from blitzecdn.capabilities.deployments.service.convergence import DeploymentService
from blitzecdn.capabilities.edges import EdgeOperationsService
from blitzecdn.capabilities.edges.domain import Edge, EdgePatch
from blitzecdn.composition import handoff_container
from blitzecdn.core.config import Settings


def finalize_standalone(
    edges: EdgeOperationsService,
    deployments: DeploymentService,
    *,
    admin_cidr: str,
    public_addresses: Sequence[str],
    deploy: bool,
) -> DeploymentStatus | None:
    """Record the local edge and optionally perform the first deployment."""
    known = {edge.name for edge in edges.list_edges()}
    addresses = tuple(public_addresses)
    if "edge-local" not in known:
        edges.add_edge(
            Edge(
                name="edge-local",
                host="localhost",
                user="deploy",
                public_addresses=addresses,
                ssh_sources=(admin_cidr,),
            ),
            "installer",
        )
    elif public_addresses:
        edges.update_edge(
            "edge-local", EdgePatch(public_addresses=addresses), "installer"
        )
    if not deploy:
        return None
    return deployments.deploy("installer").status


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blitzecdn-install-handoff")
    commands = parser.add_subparsers(dest="command", required=True)
    standalone = commands.add_parser("standalone")
    standalone.add_argument("--admin-cidr", required=True)
    standalone.add_argument("--public-address", action="append", default=[])
    standalone.add_argument("--deploy", action="store_true")
    options = parser.parse_args(arguments)

    container = handoff_container(Settings.from_environment())
    try:
        result = finalize_standalone(
            container.get(EdgeOperationsService),
            container.get(DeploymentService),
            admin_cidr=options.admin_cidr,
            public_addresses=options.public_address,
            deploy=options.deploy,
        )
    finally:
        container.close()
    if result is None:
        print("Initial deployment skipped (safe default). Run: blitzecdn deploy")
        return 0
    return 0 if result is DeploymentStatus.SUCCEEDED else 5


if __name__ == "__main__":
    raise SystemExit(main())
