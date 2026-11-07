"""The static resources built-in Nginx capabilities contribute."""

from pathlib import Path

import pytest

import blitzecdn
from blitzecdn.composition import load_registry
from blitzecdn.core.exceptions import FeatureError
from blitzecdn.core.features import (
    NginxContribution,
)
from blitzecdn.core.features.resolution import (
    resolve_nginx_resources,
)


def _template(root: Path, name: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text("# fragment\n", encoding="utf-8")


def test_nginx_resources_are_deterministic_and_keep_contexts_stable(tmp_path):
    alpha = tmp_path / "alpha"
    zulu = tmp_path / "zulu"
    _template(alpha, "alpha-server.conf.j2")
    _template(zulu, "zulu-upstream.conf.j2")
    contributions = [
        NginxContribution(
            feature="zulu",
            templates_path=zulu,
            upstream_fragments=("zulu-upstream.conf.j2",),
        ),
        NginxContribution(
            feature="alpha",
            templates_path=alpha,
            server_fragments=("alpha-server.conf.j2",),
        ),
    ]

    first = resolve_nginx_resources(contributions)
    second = resolve_nginx_resources(reversed(contributions))

    assert first == second
    assert [resource.feature for resource in first["server"]] == ["alpha"]
    assert [resource.feature for resource in first["upstream"]] == ["zulu"]


def test_two_features_cannot_own_one_nginx_resource(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _template(first, "shared.conf.j2")
    _template(second, "shared.conf.j2")

    with pytest.raises(FeatureError, match=r"shared.*alpha.*zulu"):
        resolve_nginx_resources(
            [
                NginxContribution("alpha", first, server_fragments=("shared.conf.j2",)),
                NginxContribution(
                    "zulu", second, upstream_fragments=("shared.conf.j2",)
                ),
            ]
        )


def test_a_declared_nginx_resource_must_ship_with_its_feature(tmp_path):
    with pytest.raises(FeatureError, match=r"missing.conf.j2.*is not a file"):
        resolve_nginx_resources(
            [
                NginxContribution(
                    "broken", tmp_path, server_fragments=("missing.conf.j2",)
                )
            ]
        )


def test_every_nginx_template_is_owned_by_a_feature():
    """Every Nginx fragment comes from the capability that owns it.

    Derived from what is installed rather than a list of names: core contributes
    no fragment of its own.
    """
    features = load_registry()
    owners = {feature.name for feature in features.metadata}
    resources = resolve_nginx_resources(features.nginx_contributions())
    flattened = [resource for context in resources.values() for resource in context]

    assert {resource.feature for resource in flattened} <= owners
    assert all(
        resource.template.is_relative_to(Path(blitzecdn.__file__).parent)
        for resource in flattened
    )
