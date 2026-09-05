"""Regenerate the frozen public surfaces. Run through `just refreeze`.

Deliberately a script rather than a `--update` flag on the tests. A flag is one
keystroke away from whoever is trying to get a red suite green, and these files
are the record of what this project has promised: regenerating one should be a
thing you decide to do, and it should show up in the diff of a commit as its
own act.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import frozen_surfaces as surfaces
from control_plane_fixtures import settings as _settings_fixture
from published_surface import _PUBLIC_SDK_PREFIXES


def _settings():
    """The same Settings the suite's fixture builds, without pytest running it."""
    generator = _settings_fixture.__wrapped__
    return generator(Path(tempfile.mkdtemp()))


def main() -> int:
    settings = _settings()
    generated = {
        "cli": surfaces.cli_surface(),
        "http": surfaces.http_surface(settings),
        "plugin_abi": surfaces.plugin_abi_surface(),
        "sdk": surfaces.sdk_surface(_PUBLIC_SDK_PREFIXES),
        "ansible": surfaces.ansible_surface(),
        "schema": surfaces.schema_surface(),
    }
    surfaces.FROZEN.mkdir(parents=True, exist_ok=True)
    for name in surfaces.SURFACES:
        path = surfaces.FROZEN / f"{name}.txt"
        before = path.read_text(encoding="utf-8") if path.is_file() else ""
        path.write_text(generated[name], encoding="utf-8")
        lines = len(generated[name].splitlines())
        state = "unchanged" if before == generated[name] else "UPDATED"
        print(f"  {name:12} {lines:5} lines  {state}")
    print(
        "\nRead the diff. Every line is something outside this repository can "
        "depend on."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
