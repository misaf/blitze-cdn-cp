"""Regenerate the frozen public surfaces. Run through `just refreeze`.

A script rather than a `--update` flag, so regenerating a promise is a
deliberate act that shows in its own diff.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from blitzecdn.core.config import Settings

sys.path.insert(0, str(Path(__file__).resolve().parent))

import support.frozen_surfaces as surfaces
from support.control_plane import settings as _settings_fixture


def _settings() -> Settings:
    """The same Settings the suite's fixture builds, without pytest running it."""
    # `pytest.fixture` returns a `FixtureFunctionDefinition`, which carries the
    # undecorated function at run time without declaring it.
    build: Callable[[Path], Settings] = _settings_fixture.__wrapped__  # type: ignore[attr-defined]
    return build(Path(tempfile.mkdtemp()))


def main() -> int:
    settings = _settings()
    generated = {
        "cli": surfaces.cli_surface(),
        "http": surfaces.http_surface(settings),
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
