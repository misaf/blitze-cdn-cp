"""Fixtures this distribution's role contracts need.

``desired_state`` renders the document a real deployment would converge, and
the firewall role is asserted against it. It is built in
``support/contracts.py``; a fixture has to be registered with pytest rather
than imported into the module that asks for it, hence this file.
"""

from support.contracts import desired_state

__all__ = ["desired_state"]
