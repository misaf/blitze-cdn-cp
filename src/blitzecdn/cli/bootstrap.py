"""First-run commands: scaffold local configuration, and adopt an old fleet.

``setup`` no longer creates an inventory file. The fleet lives in the ``edges``
table and Ansible reads it through the ``blitzecdn`` inventory plugin, so the
only inventory artefact is the plugin's configuration — which is tracked and
ships with the project. What ``setup`` does own is the one-time migration off
the static ``hosts.yml`` that installations before this used.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Annotated

import typer

from blitzecdn.cli import common
from blitzecdn.cli.app import app
from blitzecdn.infrastructure.inventory import (
    initialize_group_vars,
    read_legacy_inventory,
)


def _environment_file_body() -> str:
    """The scaffolded `.env`, with the optional settings named but unset.

    Commented rather than omitted: an operator who never reads the reference
    should still be able to discover that these exist, and a key that has to be
    exported into the shell before each deploy gets exported once and then
    forgotten. `.env` is written 0600 and is the intended home for both.
    """
    return (
        f"BLITZE_API_KEYS=local:{secrets.token_urlsafe(48)}\n"
        "\n"
        "# MaxMind GeoLite2, for per-hostname country filtering. Free, but the\n"
        "# download is authenticated: create an account, generate a license key,\n"
        "# and set blitzecdn_nginx_geoip_enabled in group_vars. The license key\n"
        "# is an account credential — this file is 0600 and must stay uncommitted.\n"
        "# BLITZE_MAXMIND_ACCOUNT_ID=\n"
        "# BLITZE_MAXMIND_LICENSE_KEY=\n"
    )


@app.command()
def init(
    output: Annotated[Path, typer.Option(help="Environment file to create.")] = Path(
        ".env"
    ),
) -> None:
    """Create a restrictive local environment file without overwriting one."""
    if output.exists():
        raise typer.BadParameter(f"refusing to overwrite {output}")
    output.write_text(_environment_file_body(), encoding="utf-8")
    output.chmod(0o600)
    typer.echo(f"Created {output} with mode 0600")


#: Where a pre-plugin installation kept its fleet, and where it goes afterwards.
LEGACY_INVENTORY = "ansible/inventory/hosts.yml"
MIGRATED_SUFFIX = ".migrated"


@app.command()
def setup() -> None:
    """Prepare local configuration, adopting an older static inventory.

    Safe to re-run. Nothing that already exists is overwritten, and the
    migration below only ever runs against a control plane whose fleet is
    empty — so a second `setup` cannot resurrect edges that were deliberately
    removed after the first one.
    """
    root = Path.cwd()
    environment_path = root / ".env"
    created: list[str] = []
    if not environment_path.exists():
        environment_path.write_text(_environment_file_body(), encoding="utf-8")
        environment_path.chmod(0o600)
        created.append(str(environment_path.relative_to(root)))
    # Runs on every setup, not only a fresh one: an existing install upgrading
    # into the group_vars directory layout needs this file too, and it is the
    # one place site configuration can go without blocking the next update.
    group_vars = initialize_group_vars(root / "ansible/inventory")
    if group_vars is not None:
        created.append(str(group_vars.relative_to(root)))
    if created:
        typer.echo(f"BlitzeCDN is ready. Created: {', '.join(created)}")
    else:
        typer.echo("BlitzeCDN is already set up; existing files were preserved.")
    _migrate_legacy_inventory(root)
    typer.echo("Next: blitzecdn edge add NAME --host ADDRESS --ssh-source YOUR_CIDR")


def _migrate_legacy_inventory(root: Path) -> None:
    """Adopt a static `hosts.yml` into the edges table, once.

    The fleet used to live in that file and now lives in the database, so an
    upgrade that did nothing here would come up with an empty fleet and a
    deploy that reports success having converged nothing. Every connection
    variable is carried across, including the ``ansible_port`` and
    ``ansible_ssh_private_key_file`` that only ever got there by hand.

    Guarded on the edges table being empty rather than on the file existing.
    An operator who has already registered edges has a fleet that is
    authoritative; importing over it would resurrect whatever the stale file
    still listed, including a host that was decommissioned on purpose.

    The file is renamed rather than deleted or left in place. Left in place it
    is the next person's source of confusion — it looks like configuration and
    is read by nothing — and deleting it throws away the only copy of a fleet
    that took real work to describe.
    """
    legacy = root / LEGACY_INVENTORY
    if not legacy.is_file():
        return
    control_plane = common.control_plane()
    if control_plane.edges.list_edges():
        typer.echo(
            f"\nNote: {LEGACY_INVENTORY} still exists but is no longer read. "
            "This control plane already has edges registered, so it was left "
            "alone rather than imported over them. Remove it once you have "
            "confirmed 'blitzecdn edge list' is right."
        )
        return

    edges = read_legacy_inventory(legacy)
    for edge in edges:
        control_plane.edges.add_edge(edge, "setup")
    migrated = legacy.with_name(legacy.name + MIGRATED_SUFFIX)
    legacy.rename(migrated)
    typer.echo(
        f"\nImported {len(edges)} edge(s) from {LEGACY_INVENTORY} into the "
        "control plane, which is now the only place the fleet is recorded. "
        f"The file has been renamed to {migrated.name} — Ansible reads the "
        "database through ansible/inventory/blitzecdn.yml now.\n"
        "Check it with: blitzecdn edge list"
    )
