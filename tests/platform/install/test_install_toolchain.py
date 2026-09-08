"""uv: the pinned toolchain the installer builds the virtualenv with."""

from __future__ import annotations

import platform
import re
import subprocess
from pathlib import Path

import pytest
from install_support import (
    BASH,
    _function,
    _script,
)

# --- uv: the pinned toolchain the installer builds the virtualenv with --------

#: Preamble for a stub curl: find the output path the way curl does.
#:
#: install.sh passes several flags before `-o`, so a stub that guesses at a
#: positional argument writes the download somewhere else entirely — and then
#: the checksum test still "passes", because a file that was never created
#: cannot match either. Parsing the flag is what makes these tests mean
#: something.
_CURL_STUB = """#!/usr/bin/env bash
out=""
while [[ $# -gt 0 ]]; do
  if [[ $1 == -o ]]; then out=$2; shift 2; else shift; fi
done
[[ -n "${out}" ]] || { echo "stub curl: no -o argument" >&2; exit 2; }
"""


def _host_uv_target() -> str:
    """The release triple `ensure_uv` should pick on the machine running this.

    Derived rather than hardcoded: these tests run on the Linux servers CI uses
    and on the macOS laptops the controller-only checkout is developed on, and
    a harness that always expected a Linux triple is exactly the assumption
    that shipped a Linux binary to a Mac.
    """
    platforms = {"Linux": "unknown-linux-gnu", "Darwin": "apple-darwin"}
    architectures = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }
    system = platforms[platform.system()]
    return f"{architectures[platform.machine()]}-{system}"


def _uv_harness(tmp_path: Path, *, curl_body: str, digest: str = "0" * 64) -> Path:
    """A runnable script holding just `die` and `ensure_uv`.

    Extracted from install.sh rather than copied, so a change to either
    function is exercised here instead of drifting away from a stale duplicate.
    The script cannot be sourced whole — its last line dispatches a subcommand.
    """
    pins = re.search(
        r"^UV_VERSION=.*?^UV_SHA256_aarch64_apple_darwin=.*?$",
        _script(),
        re.DOTALL | re.MULTILINE,
    )
    assert pins is not None, "the uv pins are no longer where the harness expects"
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\nset -Eeuo pipefail\n"
        f"{pins.group(0)}\n"
        "die() {\n" + _function("die") + "\n}\n"
        "sha256_of() {\n" + _function("sha256_of") + "\n}\n"
        "ensure_uv() {\n" + _function("ensure_uv") + "\n}\n"
        "ensure_uv\n",
        encoding="utf-8",
    )
    harness.chmod(0o755)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    (stub_bin / "curl").write_text(curl_body, encoding="utf-8")
    (stub_bin / "curl").chmod(0o755)
    # install.sh calls sha256sum, which is right for the Debian and Ubuntu
    # servers it installs onto but absent on a macOS developer machine. Stubbed
    # so the test runs the same everywhere: what is under test is the
    # comparison and the refusal, not the hashing.
    (stub_bin / "sha256sum").write_text(
        f'#!/usr/bin/env bash\nprintf "%s  %s\\n" "{digest}" "$1"\n',
        encoding="utf-8",
    )
    (stub_bin / "sha256sum").chmod(0o755)
    return harness


def _run_harness(tmp_path: Path, harness: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed executable and generated script
        [BASH, str(harness)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        # Deliberately not the caller's PATH: a developer with uv installed
        # would otherwise satisfy the lookup and skip the code under test.
        env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def test_the_installer_pins_uv_and_a_checksum_for_every_architecture():
    """An unpinned download is an unreviewed dependency running as root."""
    script = _script()
    assert re.search(r'^UV_VERSION="\d+\.\d+\.\d+"$', script, re.MULTILINE)
    for triple in (
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
        "x86_64-apple-darwin",
        "aarch64-apple-darwin",
    ):
        name = "UV_SHA256_" + triple.replace("-", "_")
        assert re.search(rf'^{name}="[0-9a-f]{{64}}"$', script, re.MULTILINE), (
            f"no pinned checksum for {triple}"
        )
    # Downloaded over HTTPS with the protocol pinned, so a redirect to plain
    # HTTP cannot silently downgrade the fetch.
    assert "--proto '=https' --tlsv1.2" in script


def test_a_uv_download_that_fails_its_checksum_is_refused(tmp_path: Path):
    """The whole point of pinning: a tampered archive must never be executed."""
    harness = _uv_harness(
        tmp_path,
        # Writes a well-formed file that is simply not the pinned artifact.
        curl_body=_CURL_STUB + 'printf "not the real uv" > "${out}"\n',
    )

    result = _run_harness(tmp_path, harness)

    assert result.returncode != 0
    assert "does not match its pinned checksum" in result.stderr
    assert not (tmp_path / ".state/bin/uv").exists(), "refused, but installed anyway"


@pytest.mark.parametrize(
    ("system", "machine", "triple"),
    [
        ("Linux", "x86_64", "x86_64-unknown-linux-gnu"),
        ("Linux", "aarch64", "aarch64-unknown-linux-gnu"),
        ("Darwin", "x86_64", "x86_64-apple-darwin"),
        ("Darwin", "arm64", "aarch64-apple-darwin"),
    ],
)
def test_the_download_follows_the_host_os_not_only_its_architecture(
    tmp_path: Path, system: str, machine: str, triple: str
):
    """A Linux build unpacked on a Mac passes its checksum and then cannot run.

    The failure surfaces as `cannot execute binary file` well after the point
    that was supposed to catch a wrong artifact, so the triple has to be right
    before the download, not discovered after it.
    """
    harness = _uv_harness(
        tmp_path,
        # Records the URL, then writes something that fails the checksum: what
        # is under test is which artifact was asked for.
        curl_body=(
            '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "${HOME}/requested-url"\n'
            + _CURL_STUB
            + 'printf "not the real uv" > "${out}"\n'
        ),
    )
    uname = tmp_path / "bin" / "uname"
    uname.write_text(
        "#!/usr/bin/env bash\n"
        f'case "$1" in -s) echo {system} ;; -m) echo {machine} ;; esac\n',
        encoding="utf-8",
    )
    uname.chmod(0o755)

    result = _run_harness(tmp_path, harness)

    assert result.returncode != 0, "the stub archive should have failed its checksum"
    assert f"uv-{triple}.tar.gz" in (tmp_path / "requested-url").read_text()


def test_an_unsupported_os_says_so_instead_of_downloading_a_linux_build(
    tmp_path: Path,
):
    """Naming the platform is the difference between a fix and a puzzle."""
    harness = _uv_harness(
        tmp_path,
        curl_body='#!/usr/bin/env bash\necho "curl must not run" >&2\nexit 1\n',
    )
    uname = tmp_path / "bin" / "uname"
    uname.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in -s) echo FreeBSD ;; -m) echo x86_64 ;; esac\n',
        encoding="utf-8",
    )
    uname.chmod(0o755)

    result = _run_harness(tmp_path, harness)

    assert result.returncode != 0
    assert "no pinned uv build for FreeBSD" in result.stderr


def test_an_existing_recent_uv_is_used_rather_than_downloaded(tmp_path: Path):
    """A host that already manages uv must not grow a second copy."""
    harness = _uv_harness(
        tmp_path,
        curl_body='#!/usr/bin/env bash\necho "curl must not run" >&2\nexit 1\n',
    )
    stub_uv = tmp_path / "bin" / "uv"
    # Far above any version this installer will pin.
    stub_uv.write_text('#!/usr/bin/env bash\necho "uv 99.0.0"\n', encoding="utf-8")
    stub_uv.chmod(0o755)

    result = _run_harness(tmp_path, harness)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(stub_uv)
    assert not (tmp_path / ".state/bin/uv").exists()


def test_a_verified_uv_download_returns_only_its_path(tmp_path: Path):
    """The function's stdout is its return value, so nothing else may go there.

    A progress message printed to stdout is substituted into the caller's
    `uv="$(ensure_uv)"` along with the path, and the install then fails on a
    command name with a sentence in front of it.
    """
    target = _host_uv_target()
    name = "UV_SHA256_" + target.replace("-", "_")
    expected = re.search(rf'^{name}="([0-9a-f]{{64}})"$', _script(), re.MULTILINE)
    assert expected is not None
    harness = _uv_harness(
        tmp_path,
        # A real tar laid out the way the release is: one directory named for
        # the target, holding the binary ensure_uv strips a component off.
        curl_body=(
            _CURL_STUB + 'work="$(mktemp -d)"\n'
            f'mkdir -p "${{work}}/uv-{target}"\n'
            'printf "#!/bin/sh\\necho uv 0.0.0\\n" '
            f'> "${{work}}/uv-{target}/uv"\n'
            f'tar -czf "${{out}}" -C "${{work}}" uv-{target}\n'
        ),
        digest=expected.group(1),
    )

    result = _run_harness(tmp_path, harness)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(tmp_path / ".state/bin/uv")
    assert (tmp_path / ".state/bin/uv").is_file()
