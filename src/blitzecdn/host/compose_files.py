"""Root-only configuration helpers for the host Compose wrapper.

These run in the installed image without a Docker socket. The host shell owns
container lifecycle; this module only prepares and publishes mounted files.
"""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from blitzecdn.core.domain.environment import parse_environment
from blitzecdn.core.exceptions import ConfigurationError


def publish_file(target: Path, content: bytes, *, mode: int) -> bool:
    """Replace on the destination filesystem, leaving unchanged mounts intact."""
    if target.is_file() and target.read_bytes() == content:
        return False
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}-"
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            os.fchmod(output.fileno(), mode)
            os.fchown(output.fileno(), 0, 0)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def environment(config: Path) -> bool:
    """Use the application's literal, first-assignment-wins format in Compose.

    Compose's raw env_file format takes these already unquoted values verbatim;
    it must not interpolate dollars or interpret quotes a second time.
    """
    source = config / "blitzecdn.env"
    if source.is_symlink() or not source.is_file():
        raise ConfigurationError(f"Missing environment file: {source}")
    values = parse_environment(source.read_text(), source=str(source))
    content = "".join(f"{name}={value}\n" for name, value in values.items()).encode()
    return publish_file(config / "compose.env", content, mode=0o600)


def stage_configuration(config: Path, stage: Path) -> None:
    for filename in ("blitzecdn.toml", "blitzecdn.env"):
        target = stage / filename
        shutil.copyfile(config / filename, target)
        target.chmod(0o600)
        os.chown(target, 65534, 65534)
    (stage / ".state").mkdir()
    os.chown(stage / ".state", 65534, 65534)
    os.chown(stage, 65534, 65534)


def publish_configuration(config: Path, stage: Path) -> None:
    for filename in ("blitzecdn.toml", "blitzecdn.env"):
        publish_file(
            config / filename,
            (stage / filename).read_bytes(),
            mode=0o644 if filename == "blitzecdn.toml" else 0o600,
        )


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("environment")
    for action in ("stage", "publish"):
        commands.add_parser(action).add_argument("directory", type=Path)
    options = parser.parse_args(arguments)
    try:
        if options.command == "environment":
            if environment(options.config_dir):
                print("Updated Compose environment")
        elif options.command == "stage":
            stage_configuration(options.config_dir, options.directory)
        else:
            publish_configuration(options.config_dir, options.directory)
    except (OSError, ConfigurationError) as error:
        parser.exit(1, f"blitzecdn: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
