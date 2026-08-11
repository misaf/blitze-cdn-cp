"""BlitzeCDN control plane."""

from importlib.metadata import version

__version__ = version("blitzecdn")

#: The blitzecdn.edge collection version this control plane deploys.
#:
#: The authoritative pin lives in ``ansible/requirements.yml``, which is not
#: shipped in the wheel. Consumers that only have the installed package — the
#: documentation site maintains its role reference against the edge collection
#: and must document the one this release actually deploys — read it from here.
#: ``tests/test_contract.py`` keeps the two in step.
EDGE_COLLECTION_VERSION = "1.9.1"
