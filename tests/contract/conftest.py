"""Fixtures for the contract modules under this directory.

``desired_state`` is built in ``support/contracts.py`` and registered here;
``edge/conftest.py`` overrides it for the edge modules.
"""

from support.contracts import desired_state

__all__ = ["desired_state"]
