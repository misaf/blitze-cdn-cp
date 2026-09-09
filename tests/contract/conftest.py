"""Fixtures for the contract modules under this directory.

``desired_state`` is built in ``contract_support.py``, beside the loaders and
the seeding it uses. A fixture has to be *registered* with pytest rather than
imported into the module that asks for it, though, so the name is re-exported
here — the same arrangement ``edge/conftest.py`` uses, which overrides this one
for the edge modules with a document shaped for them.
"""

from contract_support import desired_state

__all__ = ["desired_state"]
