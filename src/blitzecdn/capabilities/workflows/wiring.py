"""How the workflow journal and its coordinator are built."""

from __future__ import annotations

from dishka import Provider, Scope, alias, provide

from blitzecdn.capabilities.workflows.adapters.persistence import WorkflowStore
from blitzecdn.capabilities.workflows.ports import WorkflowHistory, WorkflowJournal
from blitzecdn.capabilities.workflows.service import WorkflowCoordinator
from blitzecdn.core.config import Settings
from blitzecdn.core.persistence.engine import Database
from blitzecdn.core.ports import UnitOfWork
from blitzecdn.core.ports.clock import Clock
from blitzecdn.core.ports.operations import EventRecorder

__all__ = ["WorkflowsProvider"]


class WorkflowsProvider(Provider):
    """The journal, read through its history port, and the one coordinator."""

    scope = Scope.APP

    @provide
    def store(self, database: Database) -> WorkflowStore:
        return WorkflowStore(database)

    journal = alias(source=WorkflowStore, provides=WorkflowJournal)
    history = alias(source=WorkflowStore, provides=WorkflowHistory)

    @provide
    def coordinator(
        self,
        settings: Settings,
        journal: WorkflowJournal,
        uow: UnitOfWork,
        events: EventRecorder,
        clock: Clock,
    ) -> WorkflowCoordinator:
        return WorkflowCoordinator(
            journal=journal,
            uow=uow,
            events=events,
            retention=settings.history_retention,
            clock=clock,
        )
