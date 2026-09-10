"""This capability's contact with the database."""

from __future__ import annotations

from blitzecdn.capabilities.jobs.adapters.persistence import JobStore, ScheduleStore

__all__ = ["JobStore", "ScheduleStore"]
