"""What the fleet's origin-check answer means.

Through the real adapter, `OriginCheckPlaybook` over `FakeRunner`'s
`run_playbook`, so a change to the variable the role reads fails a test.
"""

from __future__ import annotations

from support.control_plane import (
    FakeRunner,
    ansible_run,
    build_application,
    host_run,
    origin_report,
    seed_site,
)

from blitzecdn.composition import Repository


def origin_checks(
    fake: FakeRunner,
) -> list[tuple[list[dict[str, object]], str | None]]:
    """Every origin check the fleet was asked to run, in this capability's terms.

    `FakeRunner` records only `run_playbook(name, playbook, variables, limit)`;
    this translates that back.
    """
    return [
        (list(variables["blitzecdn_origins_sites"]), limit)
        for name, _playbook, variables, limit in fake.playbooks
        if name == "origin-check"
    ]


def test_origins_are_probed_by_the_edges_not_the_controller(settings, site_payload):
    """The check runs on the machines that carry the traffic.

    The controller's routes and egress are not the fleet's, so an origin may
    answer one and not the other. The controller only describes the origin (port
    and SNI), so the two probes agree on what it is.
    """
    repository = Repository(settings.database_path)
    fake = FakeRunner([ansible_run(origin_report("edge-a"), origin_report("edge-b"))])
    control = build_application(settings=settings, repository=repository, runner=fake)
    seed_site(control)

    report = control.origins.check_origins("alice", host_limit="edge-*")

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
    control = build_application(
        settings=settings,
        repository=repository,
        runner=FakeRunner(
            [
                ansible_run(
                    origin_report("edge-a"),
                    origin_report("edge-b", reachable=False, detail="timed out"),
                )
            ]
        ),
    )
    seed_site(control)

    report = control.origins.check_origins("alice")

    assert report.healthy is False
    assert report.failing_sites == {"example-com": ("edge-b",)}
    assert report.silent == ()


def test_a_silent_edge_is_not_a_passing_edge(settings, site_payload):
    """An edge that said nothing has not confirmed anything."""
    repository = Repository(settings.database_path)
    control = build_application(
        settings=settings,
        repository=repository,
        runner=FakeRunner([ansible_run(origin_report("edge-a"), host_run("edge-b"))]),
    )
    seed_site(control)

    report = control.origins.check_origins("alice")

    assert [edge.host for edge in report.silent] == ["edge-b"]
    assert report.silent[0].error == "the edge published no report"


def test_a_disabled_site_is_not_probed(settings, site_payload):
    """The edge will not proxy to it, so its origin being down says nothing."""
    repository = Repository(settings.database_path)
    fake = FakeRunner([ansible_run(origin_report("edge-a"))])
    control = build_application(settings=settings, repository=repository, runner=fake)
    seed_site(control, enabled=False)

    control.origins.check_origins("alice")

    sent, _limit = origin_checks(fake)[0]
    assert sent == []


def test_the_check_takes_no_deployment_lock(settings, site_payload):
    """An origin check that had to wait for a deploy is useless before one."""
    repository = Repository(settings.database_path)
    fake = FakeRunner([ansible_run(origin_report("edge-a"))])
    control = build_application(settings=settings, repository=repository, runner=fake)
    seed_site(control)

    control.origins.check_origins("alice")

    assert fake.validated == []
    assert fake.check_modes == []
