"""A configuration bundle is a value with the edge seal's identity."""

from __future__ import annotations

import hashlib

import pytest

from blitzecdn.core.domain.configuration import ConfigurationBundle


def _bundle(**links: str) -> ConfigurationBundle:
    return ConfigurationBundle(
        files={"nginx.conf": b"events {}\n", "nginx/conf.d/a.conf": b""},
        links=links,
    )


def test_the_manifest_is_the_format_the_edge_seals_a_candidate_with():
    bundle = _bundle(**{"nginx/sites-enabled/a.conf": "/etc/x/a.conf"})

    assert bundle.manifest() == {
        "nginx.conf": hashlib.sha256(b"events {}\n").hexdigest(),
        "nginx/conf.d/a.conf": hashlib.sha256(b"").hexdigest(),
        "nginx/sites-enabled/a.conf": "link:/etc/x/a.conf",
    }


def test_the_digest_follows_content_and_links():
    assert _bundle().digest == _bundle().digest
    assert _bundle().digest != _bundle(**{"x": "/y"}).digest


@pytest.mark.parametrize("path", ["/etc/nginx.conf", "../escape", "a/../../b", ""])
def test_a_path_outside_the_candidate_is_refused(path):
    with pytest.raises(ValueError, match="not inside the candidate"):
        ConfigurationBundle(files={"nginx.conf": b"", path: b""})


def test_a_bundle_without_an_entrypoint_is_refused():
    with pytest.raises(ValueError, match=r"no nginx\.conf"):
        ConfigurationBundle(files={"modules.conf": b""})


def test_a_path_cannot_be_both_a_file_and_a_link():
    with pytest.raises(ValueError, match="both files and links"):
        ConfigurationBundle(files={"nginx.conf": b"", "a": b""}, links={"a": "/b"})
