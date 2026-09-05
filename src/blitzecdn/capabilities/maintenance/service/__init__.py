"""Running one scheduled job, and paying off what it left owing.

`jobs.py` is that: the job a plugin contributed, run on the schedule
composition gave it, and the convergence a job asks for when it changed
something an edge has to be told about.
"""

from blitzecdn.capabilities.maintenance.service.jobs import MaintenanceService

__all__ = ["MaintenanceService"]
