"""Firewall rules reaching the rendered configuration."""

from __future__ import annotations

import re

from support.edge_render import (
    nginx_hash_bucket_size,
    nginx_hash_entry_size,
    render_http_fragment,
    render_site,
)
from support.policy import site_from_settings

from blitzecdn.capabilities.dns.adapters.ansible import site_to_ansible

REQUIRES_CAPABILITIES = {
    "test_firewall_rules_reach_the_generated_configuration": ("security",),
    "test_the_acme_challenge_path_is_never_filtered": ("geoip", "security"),
    "test_an_allow_country_list_refuses_addresses_the_database_cannot_place": (
        "geoip",
        "security",
    ),
    "test_allowed_methods_refuse_every_other_method": ("security",),
    "test_allowed_paths_match_the_normalized_uri_with_escaped_entries": ("security",),
    "test_method_and_path_allowlists_combine": ("security",),
    "test_the_acme_challenge_path_is_served_under_a_path_allowlist": ("security",),
    "test_an_allowed_source_is_served_from_a_denied_country": ("geoip", "security"),
    "test_an_allowed_source_is_served_outside_the_allowed_countries": (
        "geoip",
        "security",
    ),
    "test_an_allowed_source_is_exempt_from_method_rules": ("security",),
    "test_an_allowed_source_still_meets_the_path_rules": ("security",),
    "test_the_exemption_geo_is_declared_for_each_site_that_reads_it": ("security",),
    "test_the_exemption_geo_of_the_longest_site_name_fits_nginx": ("security",),
    "test_denied_paths_are_case_sensitive_by_default": ("security",),
    "test_case_insensitive_denied_paths_refuse_every_spelling": ("security",),
    "test_case_insensitive_allowed_paths_admit_every_spelling": ("security",),
    "test_case_insensitive_paths_keep_the_acme_challenge_unfiltered": ("security",),
}


def _site(firewall, name="allow"):
    return site_to_ansible(
        site_from_settings(
            {
                "name": name,
                "server_names": [f"{name}.example.com"],
                "origin_host": "origin.example.com",
                "firewall": firewall,
            }
        )
    )


def _render(firewall, **overrides):
    return render_site(_site(firewall), **overrides)


def _verdict(rendered, *, exempt, country="", method="GET", uri="/"):
    """What `location /`'s rewrite-phase rules answer, evaluated as Nginx would.

    The firewall's `set` and `if` lines run in order; the first `return` wins.
    `exempt` is the value the site's geo yields for the client address. The
    patterns are plain PCRE, which Python's `re` reads the same way.
    """
    location = rendered[rendered.index("    location / {") :]
    location = location[: location.index("try_files")]
    variables = {
        "$blitzecdn_country": country,
        "$request_method": method,
        "$uri": uri,
    }
    for line in location.splitlines():
        line = line.strip()
        if match := re.fullmatch(r'set (\$\w+) "(.*)";', line):
            name, value = match.groups()
            value = re.sub(
                r"\$blitzecdn_firewall_exempt_\w+", "1" if exempt else "0", value
            )
            for variable, current in variables.items():
                value = value.replace(variable, current)
            variables[name] = value
        elif match := re.fullmatch(
            r'if \((\$\w+) (!?~)(\*?) "(.*)"\) \{ return (\d+); \}', line
        ):
            name, operator, caseless, pattern, status = match.groups()
            flags = re.IGNORECASE if caseless else 0
            matched = re.search(pattern, variables[name], flags) is not None
            if matched == (operator == "~"):
                return int(status)
    return 200


def test_firewall_rules_reach_the_generated_configuration(desired_state):
    site = next(
        entry
        for entry in desired_state["blitzecdn_nginx_sites"]
        if entry.get("firewall")
    )
    rendered = render_site(site)
    assert "allow 203.0.113.9/32;" in rendered
    assert "deny 203.0.113.0/24;" in rendered
    assert "deny 2001:db8::/32;" in rendered
    # allow_sources exempts from the method rule too, so it reads the guard.
    assert 'if ($blitzecdn_firewall_method ~ "^0:(DELETE|TRACE)$")' in rendered
    assert "location ^~ /admin {" in rendered
    # The allow has to precede the denies: nginx takes the first match, so the
    # reverse order would make every exemption dead.
    assert rendered.index("allow 203.0.113.9/32;") < rendered.index(
        "deny 203.0.113.0/24;"
    )


def test_a_site_without_firewall_rules_renders_exactly_as_before(desired_state):
    """Every existing site must be untouched by the new block."""
    site = next(
        entry
        for entry in desired_state["blitzecdn_nginx_sites"]
        if not entry.get("firewall")
    )
    rendered = render_site(site)
    for directive in ("allow ", "deny ", "$blitzecdn_country", "$request_method ~"):
        assert directive not in rendered


def test_the_acme_challenge_path_is_never_filtered():
    """A rule that blocked renewal would surface weeks later, at expiry."""
    site = site_to_ansible(
        site_from_settings(
            {
                "name": "locked",
                "server_names": ["locked.example.com"],
                "origin_host": "origin.example.com",
                "firewall": {
                    "deny_sources": ["0.0.0.0/0", "::/0"],
                    "denied_countries": ["RU"],
                    "denied_methods": ["GET"],
                },
            }
        )
    )
    rendered = render_site(site, blitzecdn_edge_geoip_enabled=True)
    challenge = rendered.index("location ^~ /.well-known/acme-challenge/ {")
    block_end = rendered.index("}", rendered.index("try_files $uri =404;"))
    challenge_block = rendered[challenge:block_end]
    for directive in ("deny ", "$blitzecdn_country", "$request_method ~"):
        assert directive not in challenge_block, (
            f"{directive!r} applies to the ACME challenge location, so a site "
            "can filter out its own certificate authority"
        )
    # …while the site itself really is closed.
    assert "deny 0.0.0.0/0;" in rendered
    assert 'if ($blitzecdn_country ~ "^(RU)$")' in rendered


def test_an_allow_country_list_refuses_addresses_the_database_cannot_place():
    """`""` is what geoip2 yields for an unknown address.

    An allow list treats it as "not one of these", which only the negated match
    does; a deny list must not fire on it. The asymmetry is easy to invert.
    """
    site = site_to_ansible(
        site_from_settings(
            {
                "name": "geo",
                "server_names": ["geo.example.com"],
                "origin_host": "origin.example.com",
                "firewall": {"allowed_countries": ["DE", "FR"]},
            }
        )
    )
    rendered = render_site(site, blitzecdn_edge_geoip_enabled=True)
    assert 'if ($blitzecdn_country !~ "^(DE|FR)$")' in rendered


def test_allowed_methods_refuse_every_other_method():
    rendered = _render({"allowed_methods": ["GET"]})
    assert 'if ($request_method !~ "^(GET|HEAD)$") { return 405; }' in rendered
    assert "$uri !~" not in rendered


def test_allowed_paths_match_the_normalized_uri_with_escaped_entries():
    """$uri is decoded and slash-merged, so /%61pi/ and //api/ match like /api/.

    $request_uri is the raw line: matching it would let either spelling past.
    A trailing '/' is a prefix; anything else is exact, and a '.' is a dot.
    """
    rendered = _render({"allowed_paths": ["/api/", "/status", "/v1.0/"]})
    assert 'if ($uri !~ "^(/api/|/status$|/v1\\.0/)") { return 403; }' in rendered
    assert "$request_uri !~" not in rendered
    assert "$request_method !~" not in rendered


def test_method_and_path_allowlists_combine():
    rendered = _render(
        {
            "allowed_methods": ["GET"],
            "allowed_paths": ["/api/"],
            "denied_paths": ["/api/admin"],
        }
    )
    location = rendered[rendered.index("    location / {") :]
    location = location[: location.index("try_files")]
    assert '$request_method !~ "^(GET|HEAD)$"' in location
    assert '$uri !~ "^(/api/)"' in location
    # The carve-out is its own longer prefix location, so it beats `location /`.
    assert "location ^~ /api/admin { return 403; }" in rendered


def test_the_acme_challenge_path_is_served_under_a_path_allowlist():
    rendered = _render({"allowed_paths": ["/api/"], "allowed_methods": ["POST"]})
    challenge = rendered.index("location ^~ /.well-known/acme-challenge/ {")
    block_end = rendered.index("}", rendered.index("try_files $uri =404;"))
    challenge_block = rendered[challenge:block_end]
    assert "$uri !~" not in challenge_block
    assert "$request_method !~" not in challenge_block
    assert '$uri !~ "^(/api/)"' in rendered


def test_an_allowed_source_is_served_from_a_denied_country():
    """The exemption runs in the rewrite phase, where the country rule does.

    `allow` alone is read in the later access phase, so a country `return 403`
    would refuse the office network before its `allow` was ever consulted.
    """
    rendered = _render(
        {"allow_sources": ["203.0.113.0/24"], "denied_countries": ["RU"]},
        blitzecdn_edge_geoip_enabled=True,
    )
    assert _verdict(rendered, exempt=True, country="RU") == 200
    assert _verdict(rendered, exempt=False, country="RU") == 403
    assert _verdict(rendered, exempt=False, country="DE") == 200
    assert _verdict(rendered, exempt=False, country="") == 200


def test_an_allowed_source_is_served_outside_the_allowed_countries():
    rendered = _render(
        {"allow_sources": ["2001:db8::/48"], "allowed_countries": ["DE", "FR"]},
        blitzecdn_edge_geoip_enabled=True,
    )
    assert _verdict(rendered, exempt=True, country="US") == 200
    assert _verdict(rendered, exempt=True, country="") == 200
    assert _verdict(rendered, exempt=False, country="US") == 403
    # An address the database cannot place is still refused by an allow list.
    assert _verdict(rendered, exempt=False, country="") == 403
    assert _verdict(rendered, exempt=False, country="FR") == 200


def test_an_allowed_source_is_exempt_from_method_rules():
    denied = _render({"allow_sources": ["198.51.100.7"], "denied_methods": ["POST"]})
    assert _verdict(denied, exempt=True, method="POST") == 200
    assert _verdict(denied, exempt=False, method="POST") == 405
    allowed = _render({"allow_sources": ["198.51.100.7"], "allowed_methods": ["GET"]})
    assert _verdict(allowed, exempt=True, method="DELETE") == 200
    assert _verdict(allowed, exempt=False, method="DELETE") == 405
    assert _verdict(allowed, exempt=False, method="HEAD") == 200


def test_an_allowed_source_still_meets_the_path_rules():
    """Path rules describe what the site publishes, not who may ask.

    denied_paths render as their own `location ^~ … { return 403; }`, which an
    exemption could not bypass without a second copy of the proxy, and
    allowed_paths follow them so the two path rules agree.
    """
    rendered = _render(
        {
            "allow_sources": ["198.51.100.7"],
            "allowed_paths": ["/api/"],
            "denied_paths": ["/api/admin"],
        }
    )
    assert _verdict(rendered, exempt=True, uri="/private") == 403
    assert "location ^~ /api/admin { return 403; }" in rendered


def test_the_exemption_geo_is_declared_for_each_site_that_reads_it():
    """A variable read but never declared fails `nginx -t` on every edge."""
    exempted = _site(
        {
            "allow_sources": ["203.0.113.0/24", "2001:db8::/48"],
            "denied_countries": ["RU"],
        },
        name="example-com--office",
    )
    plain = _site({"denied_methods": ["TRACE"]}, name="plain")
    snippet = render_http_fragment("security-http.conf.j2", [exempted, plain])
    assert "geo $blitzecdn_firewall_exempt_example_com__office {" in snippet
    assert "    default 0;" in snippet
    assert "    203.0.113.0/24 1;" in snippet
    assert "    2001:db8::/48 1;" in snippet
    assert "$blitzecdn_firewall_exempt_plain" not in snippet
    declared = set(re.findall(r"geo (\$blitzecdn_firewall_exempt_\w+)", snippet))
    read = set(
        re.findall(
            r"\$blitzecdn_firewall_exempt_\w+",
            render_site(exempted, blitzecdn_edge_geoip_enabled=True)
            + render_site(plain),
        )
    )
    assert read == declared


def test_the_exemption_geo_of_the_longest_site_name_fits_nginx():
    """A 63-character site name made a geo name Nginx's default hash refused.

    `nginx -t` failed with "could not build variables_hash" on every edge
    once one such site listed allow_sources.
    """
    name = "a" + "-b" * 31
    assert len(name) == 63
    site = _site(
        {"allow_sources": ["203.0.113.0/24"], "denied_methods": ["TRACE"]},
        name=name,
    )
    snippet = render_http_fragment("security-http.conf.j2", [site])
    declared = set(re.findall(r"geo \$(blitzecdn_firewall_exempt_\w+)", snippet))
    read = set(re.findall(r"\$(blitzecdn_firewall_exempt_\w+)", render_site(site)))
    assert read == declared == {"blitzecdn_firewall_exempt_" + name.replace("-", "_")}
    bucket = nginx_hash_bucket_size("variables_hash_bucket_size")
    for variable in declared:
        assert 64 < nginx_hash_entry_size(variable) <= bucket


def _denied_by_location(rendered, uri):
    """Whether a denied_paths location answers `uri`, chosen as Nginx would.

    The longest prefix wins; a `^~` one ends the search, otherwise the regex
    locations are tried in order and the first match wins. Exact locations
    are left out: none of them is a path a rule could name.
    """
    prefixes = re.findall(r"^    location (\^~ )?(/\S*) \{", rendered, re.MULTILINE)
    best = max(
        ((modifier, path) for modifier, path in prefixes if uri.startswith(path)),
        key=lambda candidate: len(candidate[1]),
    )
    if best[0]:
        return f"location ^~ {best[1]} {{ return 403; }}" in rendered
    for pattern in re.findall(
        r'^    location ~\* "(.*)" \{ return 403; \}', rendered, re.MULTILINE
    ):
        if re.search(pattern, uri, re.IGNORECASE):
            return True
    return False


def test_denied_paths_are_case_sensitive_by_default():
    """The documented default: a path matches exactly as it is written."""
    rendered = _render({"denied_paths": ["/admin"]})
    assert "location ^~ /admin { return 403; }" in rendered
    assert "location ~*" not in rendered
    assert _denied_by_location(rendered, "/admin/login")
    assert not _denied_by_location(rendered, "/Admin/login")


def test_case_insensitive_denied_paths_refuse_every_spelling():
    """An IIS or ASP.NET origin serves /ADMIN as /admin; the edge must refuse it."""
    rendered = _render(
        {"denied_paths": ["/admin", "/v1.0/secret"], "case_insensitive_paths": True}
    )
    assert 'location ~* "^/admin" { return 403; }' in rendered
    assert 'location ~* "^/v1\\.0/secret" { return 403; }' in rendered
    assert "location ^~ /admin" not in rendered
    for uri in ("/admin", "/Admin", "/ADMIN/login", "/aDmIn.php", "/V1.0/SECRET"):
        assert _denied_by_location(rendered, uri), uri
    # Still a prefix of the path, and a '.' is still only a dot.
    for uri in ("/", "/public/admin", "/v1x0/secret"):
        assert not _denied_by_location(rendered, uri), uri


def test_case_insensitive_allowed_paths_admit_every_spelling():
    """The allowlist follows the same flag, so the two path rules agree."""
    rendered = _render({"allowed_paths": ["/api/"], "case_insensitive_paths": True})
    assert 'if ($uri !~* "^(/api/)") { return 403; }' in rendered
    assert _verdict(rendered, exempt=False, uri="/API/v1") == 200
    assert _verdict(rendered, exempt=False, uri="/private") == 403
    strict = _render({"allowed_paths": ["/api/"]})
    assert _verdict(strict, exempt=False, uri="/API/v1") == 403


def test_case_insensitive_paths_keep_the_acme_challenge_unfiltered():
    """Its `^~` prefix ends the search before any regex deny is read."""
    rendered = _render(
        {"denied_paths": ["/.well-known"], "case_insensitive_paths": True}
    )
    assert not _denied_by_location(rendered, "/.well-known/acme-challenge/token")
    assert _denied_by_location(rendered, "/.Well-Known/security.txt")
