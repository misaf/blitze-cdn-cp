"""What the fleet says about the origins it proxies to.

`report.py` is one edge's checks and the fleet-wide report they compose into.
The check runs on the fleet rather than on the controller, which is the whole
argument the module carries.
"""

from blitzecdn_origins.domain.report import EdgeOriginChecks, OriginReport

__all__ = ["EdgeOriginChecks", "OriginReport"]
