"""What can be done to the fleet short of converging it.

`fleet.py` is registering, updating and removing an edge, and probing one — the
operations that change *which hosts exist*, or ask a question of one, without
running a deployment.
"""

from blitzecdn.capabilities.edges.service.fleet import EdgeOperationsService

__all__ = ["EdgeOperationsService"]
