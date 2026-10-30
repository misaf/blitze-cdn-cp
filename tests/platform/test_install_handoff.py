from support.control_plane import FakeRunner, build_application

from blitzecdn.composition import Repository
from blitzecdn.entrypoints.install_handoff import finalize_standalone


def test_finalize_standalone_registers_and_updates_the_local_edge(settings):
    control = build_application(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )

    first = finalize_standalone(
        control.edges,
        control.deployments,
        admin_cidr="198.51.100.8/24",
        public_addresses=["203.0.113.10"],
        deploy=False,
    )
    second = finalize_standalone(
        control.edges,
        control.deployments,
        admin_cidr="198.51.100.8/24",
        public_addresses=["203.0.113.11"],
        deploy=False,
    )

    assert first is second is None
    edge = control.edges.get_edge("edge-local")
    assert edge.host == "localhost"
    assert edge.ssh_sources == ("198.51.100.0/24",)
    assert edge.public_addresses == ("203.0.113.11",)
