"""Variable files handed to Ansible as ``--extra-vars @file``.

ansible-core loads an ``@file`` as a trusted template, so a string holding
``{{ ... }}`` would be evaluated wherever a play touches it — a purge URI or an
error page could run ``lookup('pipe', ...)`` as the controller. Every string is
therefore written with Ansible's ``!unsafe`` tag: plays still read, compare and
filter the values, but never render them. Nothing the control plane passes this
way is meant to be a template.

ansible-core constructs an ``!unsafe`` scalar by resolving it again as a plain
one, quoted or not, so ``!unsafe 'off'`` arrives as ``False``. A string that
would resolve to anything but a string (``off``, ``1.2``, ``~``) is written
quoted and untagged instead: none of those forms can hold a template.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from blitzecdn.core.runtime.filesystem import atomic_write_bytes

__all__ = ["read_variables", "run_variables", "write_variables"]

#: Ansible's tag for a value it must never template.
_UNSAFE = "!unsafe"


#: What a plain scalar resolves to when it is a string.
_STR = "tag:yaml.org,2002:str"


class _UnsafeDumper(yaml.SafeDumper):
    """Safe YAML whose every string, key or value, is tagged ``!unsafe``."""


def _represent_unsafe(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    implicit = dumper.resolve(yaml.ScalarNode, value, (True, False))  # type: ignore[no-untyped-call]
    if implicit != _STR:
        # Tagged, Ansible would read it back as a bool, number or null.
        return dumper.represent_str(value)
    style = "|" if "\n" in value else None
    return dumper.represent_scalar(_UNSAFE, value, style=style)


_UnsafeDumper.add_representer(str, _represent_unsafe)


class _UnsafeLoader(yaml.SafeLoader):
    """Read a variables file back as plain data, for Python rather than Ansible."""


def _construct_unsafe(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    if not isinstance(node, yaml.ScalarNode):
        raise yaml.constructor.ConstructorError(
            None, None, "only strings are written !unsafe", node.start_mark
        )
    return loader.construct_scalar(node)


_UnsafeLoader.add_constructor(_UNSAFE, _construct_unsafe)


def write_variables(
    path: Path, values: Mapping[str, Any], *, mode: int = 0o600
) -> None:
    """Atomically write ``values`` as an extra-vars file no play will template."""
    document = yaml.dump(
        dict(values),
        Dumper=_UnsafeDumper,
        sort_keys=False,
        allow_unicode=False,
    )
    atomic_write_bytes(path, document.encode("utf-8"), mode=mode)


def read_variables(path: Path) -> Any:
    """Load a file :func:`write_variables` wrote, its strings as plain ``str``."""
    # A SafeLoader with one constructor added, for the `!unsafe` scalar tag.
    text = path.read_text(encoding="utf-8")
    return yaml.load(text, Loader=_UnsafeLoader)  # noqa: S506  # nosec B506


@contextmanager
def run_variables(
    directory: Path, prefix: str, values: dict[str, object]
) -> Iterator[Path]:
    """Write this run's variables to a path no other run can reach, removed after.

    Runs that skip the deployment lock overlap routinely; with a shared file a two-URL
    purge could read another run's ``purge_all: true`` and empty every cache.
    """
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{prefix}-{uuid4().hex}.yml"
    try:
        write_variables(path, values)
        yield path
    finally:
        path.unlink(missing_ok=True)
