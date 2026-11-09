"""Every job kind the worker runs is typed by one payload model.

A payload crosses from the publisher to the worker through a table, and may
wait there across a release. The worker validates it against its kind's model
before the handler runs, so that model is the contract, and every kind needs
one: a kind with no model, a model no worker handles, or two models claiming
one kind would each fail only once a job of that kind is picked up.
"""

from __future__ import annotations

import re
from importlib import import_module

import pytest
from support.control_plane import FakeRunner, build_application
from support.paths import SOURCE
from support.stores import Stores

from blitzecdn.capabilities.jobs.domain import JobPayload
from blitzecdn.capabilities.jobs.ports import JobHandler

_DECLARES_A_PAYLOAD = re.compile(r"^class \w+\(JobPayload\):", re.MULTILINE)


def _source_payload_models() -> dict[str, type[JobPayload]]:
    """Every payload model the application declares, by kind.

    A module is imported when its source declares a direct subclass, so a
    model is found whether or not anything imports it yet.
    """
    for path in sorted(SOURCE.rglob("*.py")):
        if _DECLARES_A_PAYLOAD.search(path.read_text(encoding="utf-8")):
            parts = path.relative_to(SOURCE.parent).with_suffix("").parts
            import_module(".".join(parts).removesuffix(".__init__"))
    models: dict[str, type[JobPayload]] = {}
    pending = list(JobPayload.__subclasses__())
    while pending:
        model = pending.pop()
        pending.extend(model.__subclasses__())
        if not model.__module__.startswith("blitzecdn."):
            continue
        assert model.kind not in models, (
            f"job kind {model.kind!r} is claimed by "
            f"{models[model.kind].__qualname__} and {model.__qualname__}"
        )
        models[model.kind] = model
    return models


@pytest.fixture
def worker_handlers(settings) -> dict[str, JobHandler[JobPayload]]:
    """The handlers a started worker installs, by kind."""
    control = build_application(
        settings=settings,
        repository=Stores(settings.database_path),
        runner=FakeRunner(),
    )
    return dict(control.worker.runner.queue.handlers)


def test_every_handled_kind_is_validated_by_its_own_payload_model(worker_handlers):
    assert worker_handlers
    for kind, handler in worker_handlers.items():
        assert isinstance(handler, JobHandler), kind
        assert issubclass(handler.payload, JobPayload), kind
        assert handler.payload.kind == kind


def test_every_payload_model_is_frozen_and_refuses_unknown_fields():
    """A field the model does not know is a renamed one, not one to ignore."""
    for kind, model in _source_payload_models().items():
        assert model.model_config.get("frozen") is True, kind
        assert model.model_config.get("extra") == "forbid", kind


def test_every_payload_model_has_a_handler_in_the_worker(worker_handlers):
    """A model nothing handles is a kind the worker would fail on arrival."""
    declared = _source_payload_models()

    assert set(declared) == set(worker_handlers)
    for kind, model in declared.items():
        assert worker_handlers[kind].payload is model, kind
