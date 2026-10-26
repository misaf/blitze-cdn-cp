"""The environment-file format shared by the application and host recovery."""

from __future__ import annotations

import re
import shlex

from blitzecdn.core.exceptions import ConfigurationError

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_environment(text: str, *, source: str) -> dict[str, str]:
    """Read shell-quoted assignments without expansion; the first name wins.

    Values containing spaces must be quoted. Dollar signs are literal, missing
    values are empty strings, and diagnostics never include credential values.
    """
    values: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not _NAME.fullmatch(name):
            raise ConfigurationError(f"invalid assignment in {source}:{number}")
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise ConfigurationError(
                f"invalid value in {source}:{number}: {exc}"
            ) from exc
        if len(parts) > 1:
            raise ConfigurationError(
                f"quote values containing spaces in {source}:{number}"
            )
        values.setdefault(name, parts[0] if parts else "")
    return values
