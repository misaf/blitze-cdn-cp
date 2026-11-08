"""The MCP servers the repository starts for contributors run a pinned release.

`.mcp.json` is committed, so every contributor who opens the repository in an
MCP-aware tool runs what it names. A package launched through `npx -y` without a
version runs whatever npm serves as newest that day, without asking — an unpinned
supply-chain dependency in a repository that otherwise pins its lockfile, Ansible
collections, Actions and base images exactly. Dependabot does not read `.mcp.json`,
so a pinned version here is bumped by hand.
"""

from __future__ import annotations

import json
import re

from support.paths import REPO_ROOT

MCP_CONFIG = REPO_ROOT / ".mcp.json"

# An npm package name, optionally scoped, followed by an exact semantic version.
_EXACT = re.compile(r"^(?:@[a-z0-9._-]+/)?[a-z0-9._-]+@\d+\.\d+\.\d+$")


def _npx_packages(server: dict) -> list[str]:
    """The package each `npx` invocation runs: its first argument that is not a flag."""
    if server.get("command") != "npx":
        return []
    return [arg for arg in server.get("args", []) if not arg.startswith("-")][:1]


def test_every_npx_server_runs_an_exact_version():
    servers = json.loads(MCP_CONFIG.read_text())["mcpServers"]
    packages = {
        name: package
        for name, server in servers.items()
        for package in _npx_packages(server)
    }

    assert packages, "expected .mcp.json to start at least one server through npx"
    for name, package in packages.items():
        assert _EXACT.match(package), f"{name} runs {package!r}, not an exact version"
