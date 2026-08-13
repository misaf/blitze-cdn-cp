import os
import subprocess

from conftest import FakeRunner

from blitzecdn.control_plane import ControlPlane
from blitzecdn.infrastructure.database import Repository
from blitzecdn.install_handoff import finalize_standalone, install_wrapper


def test_install_wrapper_pins_and_repoints_the_checkout(tmp_path):
    target = tmp_path / "bin"
    first = tmp_path / "first checkout"
    second = tmp_path / "second"

    assert install_wrapper(first, target)[1]
    wrapper = target / "blitzecdn"
    assert os.access(wrapper, os.X_OK)
    assert str(first) in wrapper.read_text(encoding="utf-8")

    assert install_wrapper(second, target)[1]
    assert str(second) in wrapper.read_text(encoding="utf-8")


def test_install_wrapper_preserves_a_foreign_command(tmp_path):
    target = tmp_path / "bin"
    target.mkdir()
    wrapper = target / "blitzecdn"
    wrapper.write_text("#!/bin/sh\necho not-ours\n", encoding="utf-8")

    message, installed = install_wrapper(tmp_path / "checkout", target)

    assert not installed
    assert wrapper.read_text(encoding="utf-8") == "#!/bin/sh\necho not-ours\n"
    assert "was not written by this installer" in message


def test_install_wrapper_quotes_a_checkout_with_spaces(tmp_path):
    project = tmp_path / "my checkout"
    target = tmp_path / "bin"
    assert install_wrapper(project, target)[1]

    result = subprocess.run(  # noqa: S603 - generated wrapper under test
        [str(target / "blitzecdn"), "--version"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert "my checkout/.venv/bin/blitzecdn" in result.stderr


def test_finalize_standalone_registers_and_updates_the_local_edge(settings):
    control = ControlPlane(
        settings=settings,
        repository=Repository(settings.database_path),
        runner=FakeRunner(),
    )  # type: ignore[arg-type]

    first = finalize_standalone(
        control,
        admin_cidr="198.51.100.8/24",
        public_addresses=["203.0.113.10"],
        deploy=False,
    )
    second = finalize_standalone(
        control,
        admin_cidr="198.51.100.8/24",
        public_addresses=["203.0.113.11"],
        deploy=False,
    )

    assert first is second is None
    edge = control.edges.get_edge("edge-local")
    assert edge.host == "localhost"
    assert edge.ssh_sources == ("198.51.100.0/24",)
    assert edge.public_addresses == ("203.0.113.11",)
