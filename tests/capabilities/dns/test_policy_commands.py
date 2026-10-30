"""Policy intent is immutable; selective edits resolve within the write boundary."""

from contextlib import contextmanager

import pytest
from pydantic import ValidationError
from support.control_plane import FakeRunner, build_application

from blitzecdn.capabilities.dns.domain import (
    Domain,
    DomainPatch,
    PatchPolicy,
)
from blitzecdn.capabilities.dns.service import DnsService
from blitzecdn.capabilities.security.contract import SiteFirewall
from blitzecdn.capabilities.security.domain import EditFirewall
from blitzecdn.capabilities.serving.contract import SitePolicy
from blitzecdn.capabilities.serving.domain import EditVisitorHeaders
from blitzecdn.composition import Repository


def test_selective_firewall_intent_covers_the_contract_lists():
    assert set(EditFirewall.model_fields) - {"clear"} == set(SiteFirewall.model_fields)


def test_patch_captures_intent_and_returns_independent_working_copies():
    raw = {"firewall": {"denied_paths": ["/admin"]}}
    patch = DomainPatch.model_validate(raw)
    command = PatchPolicy.from_patch(patch)
    raw["firewall"]["denied_paths"].append("/raw")
    patch.firewall = None
    first = command.changes(SitePolicy())
    first["firewall"]["denied_paths"].append("/decoded")
    assert command.changes(SitePolicy())["firewall"]["denied_paths"] == ["/admin"]
    with pytest.raises(ValidationError, match="frozen"):
        command.settings_json = "{}"


def test_patch_preserves_explicit_nulls_and_omitted_fields():
    command = PatchPolicy.from_patch(DomainPatch(origin_request_host=None))
    assert command.changes(SitePolicy()) == {"origin_request_host": None}
    assert PatchPolicy.from_patch(DomainPatch()).changes(SitePolicy()) == {}


def test_firewall_command_detaches_mutable_input_and_can_clear_one_list():
    paths = ["/admin"]
    command = EditFirewall.model_validate({"denied_paths": paths})
    paths.append("/later")
    assert command.denied_paths == ("/admin",)
    policy = SitePolicy.from_settings({"firewall": {"denied_paths": ["/admin"]}})
    assert EditFirewall(denied_paths=()).changes(policy)["firewall"].empty
    with pytest.raises(ValidationError, match="frozen"):
        command.clear = True


@pytest.mark.parametrize(
    "factory",
    [
        lambda: EditFirewall(),
        lambda: EditFirewall(clear=True, denied_paths=()),
        lambda: EditVisitorHeaders(),
    ],
)
def test_selective_commands_require_unambiguous_intent(factory):
    with pytest.raises(ValidationError):
        factory()


@pytest.mark.parametrize(
    ("earlier", "later", "expected"),
    [
        (
            EditFirewall(denied_paths=("/admin",)),
            EditFirewall(denied_methods=("DELETE",)),
            {"firewall": {"denied_paths": ["/admin"], "denied_methods": ["DELETE"]}},
        ),
        (
            EditVisitorHeaders(connecting_ip=False),
            EditVisitorHeaders(ip_country=True),
            {"visitor_headers": {"connecting_ip": False, "ip_country": True}},
        ),
    ],
)
def test_selective_edit_observes_another_writer_at_transaction_entry(
    settings, earlier, later, expected
):
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings, repository=repository, runner=FakeRunner()
    )
    control.dns.create_domain(Domain(name="example.com"), "setup")

    class Interfering:
        @contextmanager
        def transaction(self):
            # Deterministically put the other write after intent construction,
            # before the editor acquires its real write transaction.
            control.dns.update_domain("example.com", earlier, "bob")
            with control.transactions.transaction():
                yield

    editor = DnsService(
        zones=repository.zones,
        rules=repository.rules,
        events=control.events,
        uow=Interfering(),
    )
    saved = editor.update_domain("example.com", later, "alice")
    assert saved.policy == SitePolicy.from_settings(expected)
    events = control.audit.list_audit_events()
    assert [(event.operator, event.action) for event in events[:2]] == [
        ("alice", "domain.updated"),
        ("bob", "domain.updated"),
    ]


def test_failed_merged_validation_does_not_write_policy_or_audit(settings):
    control = build_application(settings=settings, runner=FakeRunner())
    original = Domain(
        name="example.com",
        policy=SitePolicy.from_settings({"firewall": {"allowed_countries": ["DE"]}}),
    )
    control.dns.create_domain(original, "setup")
    before = control.audit.list_audit_events()
    with pytest.raises(ValidationError, match="both list DE"):
        control.dns.update_domain(
            original.name, EditFirewall(denied_countries=("de",)), "alice"
        )
    assert control.dns.get_domain(original.name) == original
    assert control.audit.list_audit_events() == before


def test_audit_failure_rolls_back_the_policy_write(settings):
    control = build_application(settings=settings, runner=FakeRunner())
    original = control.dns.create_domain(Domain(name="example.com"), "setup")
    before = control.audit.list_audit_events()

    class BrokenRecorder:
        def record(self, event):
            raise RuntimeError("audit unavailable")

    control.dns.events = BrokenRecorder()
    with pytest.raises(RuntimeError, match="audit unavailable"):
        control.dns.update_domain(
            original.name, EditVisitorHeaders(ip_country=True), "alice"
        )
    assert control.dns.get_domain(original.name) == original
    assert control.audit.list_audit_events() == before


def test_patch_still_replaces_whole_blocks_while_selective_commands_keep_fields():
    policy = SitePolicy.from_settings(
        {
            "firewall": {"denied_paths": ["/admin"]},
            "visitor_headers": {"ip_country": True},
        }
    )
    patch = PatchPolicy.from_patch(
        DomainPatch(
            firewall={"denied_methods": ["DELETE"]},
            visitor_headers={"connecting_ip": False},
        )
    )
    replaced = policy.applying(patch.changes(policy))
    edited = policy.applying(EditFirewall(denied_methods=("DELETE",)).changes(policy))
    edited = edited.applying(EditVisitorHeaders(connecting_ip=False).changes(edited))
    assert replaced.security.firewall.denied_paths == ()
    assert replaced.headers.visitor_headers.ip_country is False
    assert edited.security.firewall.denied_paths == ("/admin",)
    assert edited.headers.visitor_headers.ip_country is True
    assert EditFirewall(clear=True).changes(policy)["firewall"].empty
