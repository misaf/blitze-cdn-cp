"""Command-line adapter package.

Nothing is imported at package import time, and that is load-bearing. Every
feature's command module does ``from blitzecdn.cli import common``, which runs
this file first — so an eager ``from blitzecdn.cli.main import app`` here meant
importing a feature's commands re-entered the module that assembles them, and
plugin discovery found itself half-loaded.

``app`` is still reachable as ``blitzecdn.cli.app`` through PEP 562, for the
published introspection surface the documentation site reads. Resolving it on
access rather than on import is what keeps the cycle broken; the root Typer and
its global options live in :mod:`blitzecdn.cli.root`, which is deliberately not
named ``app`` any more — a submodule of that name would shadow this attribute
the moment anything imported it.
"""

from typing import Any

__all__ = ["app"]


def __getattr__(name: str) -> Any:
    if name == "app":
        from blitzecdn.cli.main import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
