"""Control-plane configuration: the model, and where its values come from.

Split because the two are different jobs with different reasons to change.
:mod:`.settings` is the pydantic model — every field, its bounds and the
reason it exists. :mod:`.loading` is the chain of pydantic-settings sources — process
environment, `.env`, `blitzecdn.toml` — and the order they are merged in.

`Settings` stays importable from `blitzecdn.core.config`, which is the name
the rest of the control plane and every capability wheel already uses.
"""

from __future__ import annotations

from blitzecdn.core.config.loading import (
    MACHINE_SPECIFIC_CONFIG_KEYS,
    MACHINE_SPECIFIC_ENVIRONMENT_KEYS,
    is_portable_config_key,
    is_portable_environment_key,
)
from blitzecdn.core.config.settings import Settings

__all__ = [
    "MACHINE_SPECIFIC_CONFIG_KEYS",
    "MACHINE_SPECIFIC_ENVIRONMENT_KEYS",
    "Settings",
    "is_portable_config_key",
    "is_portable_environment_key",
]
