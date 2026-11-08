import errno
import os
import tempfile

import pytest
import yaml

from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.runtime import filesystem
from blitzecdn.core.runtime.filesystem import (
    atomic_write_bytes,
    atomic_write_yaml,
    read_log_tail,
)


def test_atomic_yaml_is_restrictive(tmp_path):
    target = tmp_path / "nested/state.yml"
    atomic_write_yaml(target, {"secret": "masked"})
    assert yaml.safe_load(target.read_text()) == {"secret": "masked"}
    assert os.stat(target).st_mode & 0o777 == 0o600


def test_atomic_yaml_refuses_symlink(tmp_path):
    real = tmp_path / "real"
    real.write_text("safe", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ConfigurationError, match="symlink"):
        atomic_write_yaml(link, {"bad": True})
    assert real.read_text(encoding="utf-8") == "safe"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["link", "real"]


def test_atomic_bytes_creates_a_private_parent_and_applies_the_mode(tmp_path):
    target = tmp_path / "keys/private.pem"
    atomic_write_bytes(target, b"key", mode=0o640)
    assert target.read_bytes() == b"key"
    assert os.stat(target).st_mode & 0o777 == 0o640
    assert os.stat(target.parent).st_mode & 0o777 == 0o700 & ~_umask()
    assert list(target.parent.iterdir()) == [target]


def test_atomic_bytes_restricts_the_mode_before_any_content_exists(
    tmp_path, monkeypatch
):
    """A reader of the temporary file never sees key material at a loose mode."""
    observed: list[tuple[int, int]] = []
    real_fchmod = os.fchmod

    def fchmod(descriptor, mode):
        observed.append((os.fstat(descriptor).st_size, mode))
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(filesystem.os, "fchmod", fchmod)
    atomic_write_bytes(tmp_path / "secret", b"material", mode=0o600)
    assert observed == [(0, 0o600)]


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


@pytest.fixture
def existing(tmp_path):
    target = tmp_path / "backup.tar"
    target.write_bytes(b"previous")
    return target


def _assert_unchanged(target) -> None:
    """The old file is whole and nothing temporary is left beside it."""
    assert target.read_bytes() == b"previous"
    assert list(target.parent.iterdir()) == [target]


def test_a_failed_write_leaves_the_destination_and_no_temporary(existing, monkeypatch):
    real_fdopen = os.fdopen

    class FullDisk:
        def __init__(self, stream) -> None:
            self._stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            self._stream.close()

        def fileno(self) -> int:
            return self._stream.fileno()

        def write(self, _payload) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

    def fdopen(descriptor, *args, **kwargs):
        return FullDisk(real_fdopen(descriptor, *args, **kwargs))

    monkeypatch.setattr(filesystem.os, "fdopen", fdopen)
    with pytest.raises(OSError, match="No space left"):
        atomic_write_bytes(existing, b"replacement")
    _assert_unchanged(existing)


def test_a_failed_fsync_leaves_the_destination_and_no_temporary(existing, monkeypatch):
    def refuse(_descriptor):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(filesystem.os, "fsync", refuse)
    with pytest.raises(OSError, match="Input/output error"):
        atomic_write_bytes(existing, b"replacement")
    _assert_unchanged(existing)


def test_a_failed_rename_leaves_the_destination_and_no_temporary(existing, monkeypatch):
    def refuse(_source, _destination):
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(filesystem.os, "replace", refuse)
    with pytest.raises(OSError, match="Cross-device link"):
        atomic_write_bytes(existing, b"replacement")
    _assert_unchanged(existing)


def test_a_refused_chmod_closes_the_temporary_descriptor(existing, monkeypatch):
    created: list[int] = []
    real_mkstemp = tempfile.mkstemp

    def mkstemp(*args, **kwargs):
        descriptor, name = real_mkstemp(*args, **kwargs)
        created.append(descriptor)
        return descriptor, name

    def refuse(_descriptor, _mode):
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(filesystem.tempfile, "mkstemp", mkstemp)
    monkeypatch.setattr(filesystem.os, "fchmod", refuse)
    with pytest.raises(PermissionError):
        atomic_write_bytes(existing, b"replacement")
    _assert_unchanged(existing)
    [descriptor] = created
    with pytest.raises(OSError) as closed:
        os.fstat(descriptor)
    assert closed.value.errno == errno.EBADF


def test_a_log_tail_degrades_to_nothing_without_a_readable_log(tmp_path):
    assert read_log_tail(None, limit=10) == ""
    assert read_log_tail(tmp_path / "missing.log", limit=10) == ""


def test_a_short_log_is_returned_whole(tmp_path):
    log = tmp_path / "run.log"
    log.write_bytes(b"  done\n")
    assert read_log_tail(str(log), limit=100) == "done"


def test_a_long_log_keeps_its_end_and_points_at_the_rest(tmp_path):
    log = tmp_path / "run.log"
    log.write_bytes(b"early\n" + b"x" * 50 + b"\nlast \xff line\n")
    tail = read_log_tail(log, limit=12)
    assert tail == f"[earlier output omitted; full log at {log}]\nlast \ufffd line"
