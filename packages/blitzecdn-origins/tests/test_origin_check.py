"""What the fleet's answer means, asserted where the play now lives.

These three moved here from the control plane's suite with the service, the
play and the role. They go through the *real* adapter — `OriginCheckPlaybook`
over the shared `FakeRunner`'s generic `run_playbook` — rather than a
hand-written `OriginCheckRunner` double, so a change to the variable name the
role reads fails a test rather than passing one written against a stub of
ourselves.
"""

from __future__ import annotations

from collections.abc import Sequence

from blitzecdn_origins.composition import build_origin_check_service
from control_plane_fixtures import (
    FakeRunner,
    ansible_run,
    host_run,
    origin_report,
    seed_site,
)

from blitzecdn.bootstrap import ControlPlane
from blitzecdn.persistence import Repository


def origin_checks(
    fake: object,
) -> list[tuple[list[dict[str, object]], str | None]]:
    """Every origin check the fleet was asked to run, in this capability's terms.

    The control plane's `FakeRunner` records `run_playbook(name, playbook,
    variables, limit)` and nothing more, because that is all core's Ansible
    adapter offers an installed distribution. Translating a probe into that
    document is this package's job, so translating it back is this package's
    test helper.
    """
    recorded: Sequence[tuple[str, object, dict[str, object], str | None]] = (
        fake.playbooks  # type: ignore[attr-defined]
    )
    return [
        (list(variables["blitzecdn_origins_sites"]), limit)  # type: ignore[call-overload]
        for name, _playbook, variables, limit in recorded
        if name == "origin-check"
    ]


def test_origins_are_probed_by_the_edges_not_the_controller(settings, site_payload):
    """The check runs on the machines that carry the traffic.

    The controller's routes, resolver and egress rules are not the fleet's: an
    origin allow-listing the edges refuses the controller while working
    perfectly, and one reachable only from the controller's subnet passed the
    old check and then 502'd on every edge. What the controller still owns is
    *describing* the origin — port and SNI — so the two probes cannot disagree
    about what a site's origin is.
    """
    repository = Repository(settings.database_path)
    fake = FakeRunner([ansible_run(origin_report("edge-a"), origin_report("edge-b"))])
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    seed_site(control)

    report = build_origin_check_service(control).check_origins(
        "alice", host_limit="edge-*"
    )

    sent, limit = origin_checks(fake)[0]
    assert limit == "edge-*"
    assert sent[0]["origin_port"] == 80
    assert sent[0]["ssl_mode"] == "off"
    assert "origin_scheme" not in sent[0]
    assert report.healthy is True
    assert [edge.host for edge in report.reporting] == ["edge-a", "edge-b"]


def test_an_origin_only_some_edges_can_reach_names_them(settings, site_payload):
    """The distinction a single vantage point could never have made."""
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner(
            [
                ansible_run(
                    origin_report("edge-a"),
                    origin_report("edge-b", reachable=False, detail="timed out"),
                )
            ]
        ),  # type: ignore[arg-type]
    )
    seed_site(control)

    report = build_origin_check_service(control).check_origins("alice")

    assert report.healthy is False
    assert report.failing_sites == {"cdn-example-com": ("edge-b",)}
    assert report.silent == ()


def test_a_silent_edge_is_not_a_passing_edge(settings, site_payload):
    """An edge that said nothing has not confirmed anything."""
    repository = Repository(settings.database_path)
    control = ControlPlane(
        settings=settings,
        repository=repository,
        runner=FakeRunner([ansible_run(origin_report("edge-a"), host_run("edge-b"))]),
    )  # type: ignore[arg-type]
    seed_site(control)

    report = build_origin_check_service(control).check_origins("alice")

    assert [edge.host for edge in report.silent] == ["edge-b"]
    assert report.silent[0].error == "the edge published no report"


def test_a_disabled_site_is_not_probed(settings, site_payload):
    """The edge will not proxy to it, so its origin being down says nothing."""
    repository = Repository(settings.database_path)
    fake = FakeRunner([ansible_run(origin_report("edge-a"))])
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    seed_site(control, enabled=False)

    build_origin_check_service(control).check_origins("alice")

    sent, _limit = origin_checks(fake)[0]
    assert sent == []


def test_the_check_takes_no_deployment_lock(settings, site_payload):
    """An origin check that had to wait for a deploy is useless before one.

    It reaches the fleet through `run_playbook`, which core documents as
    lock-free, and the shared runner records every lock it is asked for.
    """
    repository = Repository(settings.database_path)
    fake = FakeRunner([ansible_run(origin_report("edge-a"))])
    control = ControlPlane(settings=settings, repository=repository, runner=fake)  # type: ignore[arg-type]
    seed_site(control)

    build_origin_check_service(control).check_origins("alice")

    assert fake.validated == []
    assert fake.check_modes == []
