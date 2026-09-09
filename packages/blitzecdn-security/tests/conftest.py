"""Fixtures this distribution's role contracts need.

``desired_state`` renders the document a real deployment would converge, and
the firewall role is asserted against it. It is built in the control plane's
``contract_support.py``, which this suite reaches through the ``pythonpath``
entry for ``tests/``; a fixture has to be registered with pytest rather than
imported into the module that asks for it, hence this file.
"""

from contract_support import desired_state

__all__ = ["desired_state"]
