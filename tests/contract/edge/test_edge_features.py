"""Per-feature rendering: compression, cache key, uploads, websockets, TLS floor."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

# The nginx role renders from its own defaults *and* the shared edge runtime
# contract, so the loader that builds that namespace is shared with the other
# contract-test modules rather than reimplemented here.
from contract_support import (
    _role_defaults,
    _runtime_defaults,
)
from edge_render_support import (
    ROLE_DIR,
    _defaults_of,
    _render,
    _role_spec,
)
from paths import REPO_ROOT

import blitzecdn
from blitzecdn.capabilities.cache.policy import CacheQueryStringMode
from blitzecdn.capabilities.compression.policy import CompressionMode
from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible
from blitzecdn.capabilities.dns.domain import (
    CdnSite,
)
from blitzecdn.capabilities.http.policy import (
    HTTP_PROXY_PORTS,
    HTTPS_PROXY_PORTS,
    MaxUploadSize,
)
from blitzecdn.capabilities.tls.policy import (
    MinimumTlsVersion,
)

REQUIRES_CAPABILITIES = {
    "test_websocket_upgrade_is_forwarded_and_never_cached": ("cache",),
    "test_cache_query_string_mode_selects_the_cache_key": ("cache",),
    "test_brotli_uses_the_managed_filter_and_keeps_gzip_fallback": ("compression",),
    "test_compression_off_says_so_rather_than_staying_silent": ("compression",),
    "test_gzip_only_turns_the_managed_brotli_filter_off": ("compression",),
    "test_compression_leaves_the_cache_key_and_origin_request_alone": (
        "cache",
        "compression",
    ),
    "test_missing_compression_preserves_pre_upgrade_behavior": ("compression",),
}


def test_websocket_upgrade_is_forwarded_and_never_cached():
    site = CdnSite.model_validate(
        {
            "name": "socket",
            "server_names": ["socket.example.com"],
            "origin_host": "origin.example.com",
        }
    )
    rendered = _render(site_to_ansible(site))
    http_template = (ROLE_DIR / "templates/http.conf.j2").read_text(encoding="utf-8")

    assert "map $http_upgrade $blitzecdn_connection_upgrade" in http_template
    assert "proxy_set_header Upgrade $http_upgrade;" in rendered
    assert "proxy_set_header Connection $blitzecdn_connection_upgrade;" in rendered
    assert "proxy_cache_bypass $http_upgrade;" in rendered
    assert "proxy_no_cache $http_upgrade;" in rendered


@pytest.mark.parametrize(
    ("minimum", "protocols"),
    [
        (MinimumTlsVersion.TLS_1_2, "TLSv1.2 TLSv1.3"),
        (MinimumTlsVersion.TLS_1_3, "TLSv1.3"),
    ],
)
def test_minimum_tls_version_renders_per_hostname(minimum, protocols):
    site = CdnSite.model_validate(
        {
            "name": "tls-minimum",
            "server_names": ["tls.example.com"],
            "origin_host": "origin.example.com",
            "ssl_mode": "flexible",
            "minimum_tls_version": minimum,
            "certificate_mode": "existing",
            "certificate_path": "/etc/ssl/certs/edge.pem",
            "certificate_key_path": "/etc/ssl/private/edge.key",
        }
    )

    assert f"ssl_protocols {protocols};" in _render(site_to_ansible(site))


@pytest.mark.parametrize(
    ("mode", "key_uri"),
    [
        (CacheQueryStringMode.INCLUDE, "$request_uri"),
        (CacheQueryStringMode.IGNORE, "$blitzecdn_uri_without_query"),
    ],
)
def test_cache_query_string_mode_selects_the_cache_key(mode, key_uri):
    site = CdnSite.model_validate(
        {
            "name": "query-mode",
            "server_names": ["query.example.com"],
            "origin_host": "origin.example.com",
            "cache_query_string_mode": mode,
        }
    )

    rendered = _render(site_to_ansible(site))

    assert (
        f'proxy_cache_key "$scheme$server_port$request_method$host{key_uri}' in rendered
    )
    # Ignore affects identity in the cache, not what the origin receives.
    assert "proxy_pass $blitzecdn_upstream$request_uri;" in rendered


def _compressed(mode: CompressionMode) -> dict[str, Any]:
    return site_to_ansible(
        CdnSite.model_validate(
            {
                "name": "compressed",
                "server_names": ["compressed.example.com"],
                "origin_host": "origin.example.com",
                "compression": mode,
            }
        )
    )


def test_brotli_uses_the_managed_filter_and_keeps_gzip_fallback():
    site = _compressed(CompressionMode.BROTLI)
    rendered = _render(site)
    assert "brotli on;" in rendered
    assert "brotli_comp_level 5;" in rendered
    assert "gzip on;" in rendered, "Brotli never replaces the gzip fallback"


def test_compression_off_says_so_rather_than_staying_silent():
    """Debian's nginx.conf carries `gzip on` in the http context.

    Omitting the directive therefore does not mean off — it means inherited.
    """
    rendered = _render(_compressed(CompressionMode.OFF))

    assert "gzip off;" in rendered
    assert "gzip on;" not in rendered


def test_gzip_only_turns_the_managed_brotli_filter_off():
    rendered = _render(_compressed(CompressionMode.GZIP))

    assert "gzip on;" in rendered
    assert "brotli off;" in rendered
    assert "brotli on;" not in rendered


def test_compression_never_lists_text_html():
    """Both modules always compress it, and gzip warns about the duplicate.

    A warning on every `nginx -t` is how operators learn to read a noisy config
    test as normal, which is the failure this prevents rather than the
    duplicate itself.
    """
    compression_role = (
        REPO_ROOT
        / "packages/blitzecdn-compression/src/blitzecdn_compression/ansible/roles"
        / "blitzecdn_compression"
    )
    types = _defaults_of(compression_role)["blitzecdn_compression_types"]

    assert "text/html" not in types
    # Nothing already compressed: re-encoding one of these adds bytes and CPU.
    assert not {"image/jpeg", "image/png", "font/woff2", "application/zip"} & set(types)


def test_compression_leaves_the_cache_key_and_origin_request_alone():
    """The edge compresses on egress; the cache still stores what the origin sent.

    If this ever stopped holding, a cache entry would carry an encoding that
    the key does not distinguish, and a client would be handed a body it cannot
    decode — the exact failure the Accept-Encoding map exists to prevent.
    """
    rendered = _render(_compressed(CompressionMode.BROTLI))

    assert "proxy_set_header Accept-Encoding $blitzecdn_accept_encoding;" in rendered
    assert (
        'proxy_cache_key "$scheme$server_port$request_method$host$request_uri'
        '$blitzecdn_accept_encoding"' in rendered
    )
    # gzip_vary is for shared caches downstream of us, which do not know our key.
    assert "gzip_vary on;" in rendered
    # Proxied responses carry headers that disable gzip under the default.
    assert "gzip_proxied any;" in rendered


def test_managed_nginx_stack_uses_ubuntu_abi_matched_modules():
    """The stack is one ABI unit, now as an image rather than an apt transaction.

    Which makes the unit stronger rather than weaker: the binary and its three
    dynamic modules are resolved together at build time, published together,
    and pulled together as one immutable object. Nothing on an edge can pair
    one version's binary with another version's modules, because nothing on an
    edge installs either. tests/test_ansible_role_contracts.py holds the
    Dockerfile end of this; here the point is that the *edge* no longer names
    the packages at all.
    """
    assert "blitzecdn_nginx_packages" not in _role_defaults()
    assert (
        "ghcr.io/misaf/blitzecdn-edge"
        in _runtime_defaults()["blitzecdn_edge_runtime_image_default"]
    )


def test_the_pinned_edge_image_names_this_release():
    """The wheel and the edge image it deploys are one release, or neither is.

    `.github/workflows/edge-image.yml` tags what it publishes with
    `type=semver,pattern={{version}}`, so the published tag *is* the version in
    `pyproject.toml` — the two numbers were already the same fact written in
    three places, kept together by nothing but memory. They drifted: the pin sat
    at 2.7.0 for four releases after 1b89823 rebuilt the runtime on Alpine, so
    every edge validated its configuration against Ubuntu's Nginx while the
    contract beside it declared `worker_uid: 101`. That mismatch reached an
    operator as a chown failure on /var/lib/nginx/body.

    Asserted rather than derived, because a pin an operator can read and grep is
    worth more than one the controller computes — and because deriving it would
    name an image the registry does not have yet in the window between bumping
    the version and pushing the tag. `just release-version` moves all three
    together; this is what fails when something else moves one of them.
    """
    version = blitzecdn.__version__
    assert _runtime_defaults()["blitzecdn_edge_runtime_image_default"].endswith(
        f":{version}"
    ), "blitzecdn_edge/defaults/main.yml pins an edge image from another release"

    fleet = yaml.safe_load(
        (
            REPO_ROOT
            / "src/blitzecdn/ansible/inventory/group_vars/blitzecdn_edges/defaults.yml"
        ).read_text()
    )
    assert fleet["blitzecdn_edge_image_tag"] == version, (
        "the fleet group_vars tag and the standalone fallback name different releases"
    )


def test_missing_compression_preserves_pre_upgrade_behavior():
    """A running older control plane may deploy through an updated role.

    Unlike always_use_https, the safe default here is on: the role's default
    and the domain's agree on brotli, so an edge upgraded ahead of its
    controller compresses exactly as it will once both have moved.
    """
    site = _compressed(CompressionMode.BROTLI)
    del site["compression"]

    option = _role_spec()["blitzecdn_nginx_sites"]["options"]["compression"]
    assert option["default"] == "brotli"
    assert option.get("required", False) is False

    assert "gzip on;" in _render(site)


def _uploads(size: MaxUploadSize | None) -> dict[str, Any]:
    """A TLS site, so both the plain and the encrypted server block render."""
    document = site_to_ansible(
        CdnSite.model_validate(
            {
                "name": "uploads",
                "server_names": ["uploads.example.com"],
                "origin_host": "origin.example.com",
                "ssl_mode": "flexible",
                "certificate_mode": "existing",
                "certificate_path": "/etc/ssl/certs/edge.pem",
                "certificate_key_path": "/etc/ssl/private/edge.key",
                **({} if size is None else {"max_upload_size": size}),
            }
        )
    )
    if size is None:
        del document["max_upload_size"]
    return document


@pytest.mark.parametrize("size", list(MaxUploadSize))
def test_the_upload_limit_covers_every_listener_a_site_serves(size):
    """Server level, not location level.

    A limit inside `location /` alone would leave the ACME challenge, the
    reserved `/.blitzecdn` paths and every plugin fragment on nginx's 1m
    default, so a site set to 200m would still refuse a 2m upload down some
    paths and not others.
    """
    rendered = _render(_uploads(size))
    blocks = rendered.split("server {")[1:]

    # One server block per public proxy port, plain and TLS alike.
    assert len(blocks) == len(HTTP_PROXY_PORTS) + len(HTTPS_PROXY_PORTS)
    assert rendered.count(f"client_max_body_size {size.value};") == len(blocks)
    for block in blocks:
        before, _, rest = block.partition(f"client_max_body_size {size.value};")
        assert rest, "every server block carries the limit"
        assert "location" not in before, "the limit precedes every location"


def test_missing_max_upload_size_preserves_pre_upgrade_behavior():
    """A running older control plane may deploy through an updated role.

    The role's default has to be the domain's, or an edge upgraded ahead of its
    controller would serve a different limit than the one the API reports.
    """
    option = _role_spec()["blitzecdn_nginx_sites"]["options"]["max_upload_size"]
    assert option["default"] == MaxUploadSize.SMALL.value
    assert option.get("required", False) is False

    assert "client_max_body_size 100m;" in _render(_uploads(None))
