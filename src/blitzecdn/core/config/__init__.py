"""Control-plane configuration: the model, and where its values come from.

Split because the two are different jobs with different reasons to change.
:mod:`.settings` is the pydantic model — every field, its bounds and the
reason it exists. :mod:`.loading` is the precedence chain that turns a
process environment, a `.env` file and `blitzecdn.toml` into one payload for
it, which is real logic and now has somewhere to be tested on its own.

`Settings` stays importable from `blitzecdn.core.config`, which is the name
the rest of the control plane and every capability wheel already uses.
"""

from __future__ import annotations

from blitzecdn.core.config.loading import (
    MACHINE_SPECIFIC_CONFIG_KEYS,
    MACHINE_SPECIFIC_ENVIRONMENT_KEYS,
    PORTABLE_CONFIG_KEYS,
    is_portable_environment_key,
    settings_payload,
)
from blitzecdn.core.config.settings import Settings

__all__ = [
    "MACHINE_SPECIFIC_CONFIG_KEYS",
    "MACHINE_SPECIFIC_ENVIRONMENT_KEYS",
    "PORTABLE_CONFIG_KEYS",
    "Settings",
    "is_portable_environment_key",
    "settings_payload",
]
