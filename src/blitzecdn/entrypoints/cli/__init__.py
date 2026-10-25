"""Command-line adapter package; imports nothing at package import time.

Every command module imports ``common`` from here, so an eager import of the
assembled app would re-enter command loading. ``app`` is resolved lazily via
PEP 562 for the documentation site; the root Typer lives in ``root``, never a
submodule named ``app``, which would shadow it.
"""

from typing import Any

__all__ = ["app"]


def __getattr__(name: str) -> Any:
    if name == "app":
        from blitzecdn.entrypoints.cli.main import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
