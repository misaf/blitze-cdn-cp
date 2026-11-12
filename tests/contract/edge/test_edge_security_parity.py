"""The security rules written in Python and again in Jinja, compared by behaviour.

``tests/contract/test_mirrored_rules.py`` lists each rule spelled twice. The
pinning tests there compare one rendering with the Python spelling. This module
compares them across generated and boundary inputs, and checks what each side
means as well as how it is spelled.

The pairs under test and what both sides agree on:

allowed-path alternation
    ``allowed_paths_pattern`` in the contract, and the loops in
    ``security-access.conf.j2`` (``firewall.allowed_paths``) and
    ``security-http.conf.j2`` (``rate_limit.paths``). A path ending in ``/``
    covers every URI it starts. Any other path covers only itself. Every
    character is literal. ``case_insensitive_paths`` folds ASCII letters only.
    An exempt client is never counted by the rate limit, and a limit with no
    paths counts every URI.

rate-limit zone naming
    ``rate_limit_zone`` in the contract, the ``limit_req`` a site's access
    fragment reads, and the ``limit_req_zone`` the http fragment declares. Each
    rung of ``RATE_LIMIT_RATES`` has its own zone, and no two rungs share one.
    Every zone a site reads is declared once, at that site's rate. A rate off
    the ladder is refused before anything renders.

The rendered regexes run under Python's ``re``. These patterns use only literal
characters, ``|``, ``(...)``, ``^`` and ``$``, and PCRE treats those the same
way. Each check returns its counterexample rather than asserting, so a failure
names the input. The mutation tests at the end feed each check a deliberately
wrong copy to show that a drift is caught.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from functools import cache
from typing import Any

import pytest
from hypothesis import given, note, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from support.edge_render import (
    _capability_defaults,
    nginx_environment,
    nginx_resources,
    render_http_fragment,
    render_site,
    role_defaults,
)
from support.paths import SOURCE
from support.policy import site_from_settings

from blitzecdn.capabilities.security.contract import (
    ALLOWED_PATHS_PATTERN_LIMIT,
    RATE_LIMIT_RATES,
    allowed_paths_pattern,
    rate_limit_zone,
)
from blitzecdn.capabilities.sites.adapters.ansible import site_to_ansible
from blitzecdn.core.domain.values import url_path

REQUIRES_CAPABILITIES = {
    "test_the_fragments_render_here_as_they_do_in_a_site": ("security",),
    "test_allowed_path_boundaries_mean_the_same_on_both_sides": ("security",),
    "test_an_unlimited_path_list_counts_every_request": ("security",),
    "test_generated_allowed_paths_mean_the_same_on_both_sides": ("security",),
    "test_paths_the_policy_refuses_never_reach_a_template": ("security",),
    "test_a_pattern_over_the_limit_is_refused_for_both_rules": ("security",),
    "test_every_rung_names_its_own_zone_on_both_sides": ("security",),
    "test_generated_sites_declare_exactly_the_zones_they_read": ("security",),
    "test_rates_off_the_ladder_are_refused": ("security",),
    "test_a_python_side_that_drops_the_prefix_rule_is_caught": ("security",),
    "test_a_template_that_drops_the_prefix_rule_is_caught": ("security",),
    "test_a_template_that_names_zones_differently_is_caught": ("security",),
}

_NGINX = SOURCE / "capabilities/security/nginx"
ACCESS = "security-access.conf.j2"
HTTP = "security-http.conf.j2"

#: The line in both templates that spells one path's alternative.
_PATH_ALTERNATIVE = "(path | regex_escape) ~ ('' if path.endswith('/') else '$')"

_ACCESS_RULE = re.compile(
    r'if \(\$uri !~(?P<caseless>\*?) "\^\((?P<pattern>.*)\)"\) \{ return 403; \}'
)
_MAP_RULE = re.compile(
    r'"~(?P<caseless>\*?)\^0:(?:\((?P<pattern>.*)\))?" "(?P<site>[a-z0-9-]+):'
    r'\$binary_remote_addr";'
)
_ZONE_READ = re.compile(r"limit_req zone=(\S+?)[ ;]")
_ZONE_DECLARED = re.compile(
    r"^limit_req_zone \$blitzecdn_rate_limit_key zone=(\S+):5m rate=(\S+);$",
    re.MULTILINE,
)
#: Nginx's shared zone names, as `limit_req_zone` parses them.
_ZONE_NAME = re.compile(r"[A-Za-z0-9_]+")


# --- rendering -----------------------------------------------------------------


@cache
def _environment():
    return nginx_environment()


@cache
def _context() -> dict[str, Any]:
    return (
        role_defaults()
        | _capability_defaults()
        | {"blitzecdn_nginx_resources": nginx_resources()}
    )


@cache
def _source(name: str) -> str:
    return (_NGINX / name).read_text(encoding="utf-8")


def _fragment(name: str, *, source: str | None = None, **values: Any) -> str:
    """One security fragment, from its file or from ``source`` if given.

    A site's fragment is rendered alone, the same way ``site.conf.j2`` does
    it, so that a few hundred generated sites stay fast. The first test checks
    that the result matches a full site render.
    """
    template = _environment().from_string(source or _source(name))
    return template.render(**(_context() | values))


def _site(name: str = "parity", **settings: Any) -> dict[str, Any]:
    return site_to_ansible(
        site_from_settings(
            {
                "name": name,
                "server_names": [f"{name}.example.com"],
                "origin_host": "origin.example.com",
                **settings,
            }
        )
    )


# --- the alternation's semantics ----------------------------------------------

#: A rule as a test sees it: its alternation, whether it folds case, and the
#: text a request is matched against.
Rule = tuple[str, bool]


def _ascii_lower(value: str) -> str:
    return value.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )


def _covers(paths: Sequence[str], uri: str, *, caseless: bool) -> bool:
    """The shared rule itself, written as plainly as possible."""
    fold = _ascii_lower if caseless else str
    return any(
        fold(uri) == fold(path)
        or (path.endswith("/") and fold(uri).startswith(fold(path)))
        for path in paths
    )


def _matches(pattern: str, uri: str, *, caseless: bool) -> bool:
    flags = re.IGNORECASE | re.ASCII if caseless else 0
    return re.match(f"^({pattern})", uri, flags) is not None


def _probes(paths: Iterable[str]) -> list[str]:
    """URIs on each side of every path's boundary.

    Each path itself, the path with one character more or one less, a deeper
    URI, other spellings of its letters, and the path with every regex
    metacharacter replaced by a character that metacharacter would match.
    """
    found = {"/", "/unlisted", "/unlisted/deeper"}
    for path in paths:
        found |= {
            path,
            path + "x",
            path + "/",
            path + "/deeper/file.txt",
            path[:-1] or "/",
            path.rstrip("/") or "/",
            "/" + path.lstrip("/") + "x",
            "/x" + path,
            path.upper(),
            path.swapcase(),
            re.sub(r"[.+*()\[\]^$|]", "a", path),
            re.sub(r"(.)\+", r"\1\1", path),
            path.replace("|", "/"),
        }
    return sorted(uri for uri in found if uri.startswith("/"))


def _divergence(
    paths: Sequence[str],
    rule: Rule | None,
    *,
    caseless: bool,
    uris: Iterable[str] = (),
) -> str | None:
    """The first URI the rule and the shared semantics disagree on, if any."""
    if rule is None:
        return f"no rule rendered for {list(paths)!r}"
    pattern, rendered_caseless = rule
    if rendered_caseless != caseless:
        return f"case folding is {rendered_caseless}, expected {caseless}"
    for uri in [*_probes(paths), *uris]:
        expected = _covers(paths, uri, caseless=caseless)
        if _matches(pattern, uri, caseless=caseless) != expected:
            return (
                f"{uri!r} {'should' if expected else 'should not'} match "
                f"{pattern!r} for paths {list(paths)!r}"
            )
    return None


def _access_rule(rendered: str) -> Rule | None:
    found = _ACCESS_RULE.search(rendered)
    return None if found is None else (found["pattern"], bool(found["caseless"]))


def _map_rule(rendered: str, site: str) -> Rule | None:
    for found in _MAP_RULE.finditer(rendered):
        if found["site"] == site:
            return found["pattern"] or "", bool(found["caseless"])
    return None


def _map_divergence(
    paths: Sequence[str],
    rendered: str,
    site: str,
    *,
    caseless: bool,
    uris: Iterable[str] = (),
) -> str | None:
    """As :func:`_divergence`, for the map key ``<exempt>:$uri``.

    No paths counts every URI. An exempt client, keyed ``1:``, never counts.
    """
    rule = _map_rule(rendered, site)
    if rule is None:
        return f"no rate-limit map rendered for {site!r}"
    pattern, rendered_caseless = rule
    if rendered_caseless != caseless:
        return f"case folding is {rendered_caseless}, expected {caseless}"
    flags = re.IGNORECASE | re.ASCII if caseless else 0
    keyed = f"^0:({pattern})" if pattern else "^0:"
    for uri in [*_probes(paths), *uris]:
        expected = not paths or _covers(paths, uri, caseless=caseless)
        if (re.match(keyed, "0:" + uri, flags) is not None) != expected:
            return (
                f"{uri!r} {'should' if expected else 'should not'} be counted "
                f"by {keyed!r} for paths {list(paths)!r}"
            )
        if re.match(keyed, "1:" + uri, flags) is not None:
            return f"an exempt client is counted on {uri!r} by {keyed!r}"
    return None


def _parity(
    paths: Sequence[str],
    *,
    caseless: bool,
    uris: Iterable[str] = (),
    python: Callable[[Sequence[str]], str] = allowed_paths_pattern,
    access: str | None = None,
    http: str | None = None,
) -> list[str]:
    """Every disagreement between the sides for these paths, as messages.

    ``python``, ``access`` and ``http`` swap in one side's copy, which is how
    the mutation tests below feed a deliberate drift through the same check.
    """
    uris = list(uris)
    problems: list[str] = []
    python_rule = (python(paths), caseless)
    if problem := _divergence(paths, python_rule, caseless=caseless, uris=uris):
        problems.append(f"python: {problem}")

    firewall = {"allowed_paths": list(paths), "case_insensitive_paths": caseless}
    firewalled = _site(firewall=firewall)
    access_rule = _access_rule(_fragment(ACCESS, source=access, site=firewalled))
    if problem := _divergence(paths, access_rule, caseless=caseless, uris=uris):
        problems.append(f"{ACCESS}: {problem}")
    if access_rule is not None and access_rule[0] != python_rule[0]:
        problems.append(
            f"{ACCESS} spells {access_rule[0]!r}, python {python_rule[0]!r}"
        )

    limited = _site(
        rate_limit={"rate": "1r/s", "paths": list(paths)},
        firewall={"case_insensitive_paths": caseless},
    )
    rendered = _fragment(HTTP, source=http, blitzecdn_nginx_sites=[limited])
    if problem := _map_divergence(
        paths, rendered, "parity", caseless=caseless, uris=uris
    ):
        problems.append(f"{HTTP}: {problem}")
    map_rule = _map_rule(rendered, "parity")
    if map_rule is not None and paths and map_rule[0] != python_rule[0]:
        problems.append(f"{HTTP} spells {map_rule[0]!r}, python {python_rule[0]!r}")
    return problems


# --- the zone names' semantics ------------------------------------------------


def _zone_mismatches(
    rates: Sequence[str],
    *,
    python: Callable[[str], str] = rate_limit_zone,
    access: str | None = None,
    http: str | None = None,
) -> list[str]:
    """Every way the zones for these sites' rates disagree, as messages.

    Site ``site<i>`` limits to ``rates[i]``.
    """
    sites = [
        _site(name=f"site{index}", rate_limit={"rate": rate})
        for index, rate in enumerate(rates)
    ]
    problems: list[str] = []
    declared: dict[str, str] = {}
    declarations = _ZONE_DECLARED.findall(
        _fragment(HTTP, source=http, blitzecdn_nginx_sites=sites)
    )
    for zone, rate in declarations:
        if zone in declared:
            problems.append(f"zone {zone!r} is declared twice")
        declared[zone] = rate
    for site, rate in zip(sites, rates, strict=True):
        expected = python(rate)
        if not _ZONE_NAME.fullmatch(expected):
            problems.append(f"python names {rate!r} {expected!r}, not a zone name")
        read = _ZONE_READ.findall(_fragment(ACCESS, source=access, site=site))
        if read != [expected]:
            problems.append(
                f"{site['name']} at {rate!r} reads {read!r}, python {expected!r}"
            )
        if declared.get(expected) != rate:
            problems.append(
                f"{expected!r} for {rate!r} is declared at {declared.get(expected)!r}"
            )
    if set(declared) != {python(rate) for rate in rates}:
        problems.append(
            f"declared {sorted(declared)!r} for rates {sorted(set(rates))!r}"
        )
    return problems


# --- inputs -------------------------------------------------------------------

#: Letters, digits, regex metacharacters a path may hold, `%` (``$uri`` is
#: decoded, so it is literal there), and a non-ASCII letter whose case the rule
#: must not fold.
_PATH_ALPHABET = "abzAZ09/._-~%+*()[]^$|:@!,<>=&é"
_PATH = (
    st.text(alphabet=st.sampled_from(_PATH_ALPHABET), max_size=16)
    .map(lambda text: "/" + text)
    .map(url_path)
)
_PATHS = st.lists(_PATH, min_size=1, max_size=6, unique=True)
_URIS = st.lists(_PATH, max_size=8)

#: Each one a boundary: what it covers and what it must not.
BOUNDARIES = {
    "root prefix covers everything": ["/"],
    "exact path is not a prefix": ["/api"],
    "prefix does not cover its bare stem": ["/api/"],
    "a path and its prefix together": ["/api", "/api/"],
    "dot is literal": ["/a.b", "/c.d/"],
    "plus is literal": ["/a+b"],
    "star is literal": ["/a*/"],
    "alternation bar is literal": ["/a|/b"],
    "group is literal": ["/(a|b)/"],
    "class is literal": ["/[ab]"],
    "anchors are literal": ["/^x$", "/y$/"],
    "percent is literal": ["/%2e/"],
    "non-ascii path": ["/é/", "/ÉCOLE"],
    "deep prefix and exact neighbour": ["/a/b/c/", "/a/b"],
    "mixed case": ["/Login", "/Admin/"],
}


# --- tests --------------------------------------------------------------------


def test_the_fragments_render_here_as_they_do_in_a_site():
    """The standalone render the checks use is what a site's server carries."""
    site = _site(
        firewall={"allowed_paths": ["/api/", "/a+b"], "case_insensitive_paths": True},
        rate_limit={"rate": "10r/s", "paths": ["/login"]},
    )
    full = render_site(site)
    for line in _fragment(ACCESS, site=site).splitlines():
        assert line in full, line
    assert render_http_fragment(HTTP, [site]) == _fragment(
        HTTP, blitzecdn_nginx_sites=[site]
    )


@pytest.mark.parametrize("caseless", [False, True], ids=["exact-case", "caseless"])
@pytest.mark.parametrize("paths", BOUNDARIES.values(), ids=BOUNDARIES.keys())
def test_allowed_path_boundaries_mean_the_same_on_both_sides(paths, caseless):
    assert _parity(paths, caseless=caseless) == []


def test_an_unlimited_path_list_counts_every_request():
    """The one case the map spells differently: no paths, no alternation."""
    limited = _site(rate_limit={"rate": "1r/s"})
    rendered = _fragment(HTTP, blitzecdn_nginx_sites=[limited])
    assert _map_divergence([], rendered, "parity", caseless=False) is None
    assert _map_rule(rendered, "parity") == ("", False)


@settings(max_examples=150, deadline=None)
@given(paths=_PATHS, caseless=st.booleans(), uris=_URIS)
def test_generated_allowed_paths_mean_the_same_on_both_sides(paths, caseless, uris):
    note(f"pattern: {allowed_paths_pattern(paths)!r}")
    assert _parity(paths, caseless=caseless, uris=uris) == []


@pytest.mark.parametrize(
    "path",
    [
        "api",
        "",
        "/a b",
        "/a\tb",
        "/a;b",
        '/a"b',
        "/a'b",
        "/a\\b",
        "/a{b}",
        "/a#b",
        "/a?b",
        "/a`b",
        "/a\x01b",
        "/a\x7fb",
        "/" + "a" * 512,
    ],
)
@pytest.mark.parametrize("field", ["firewall.allowed_paths", "rate_limit.paths"])
def test_paths_the_policy_refuses_never_reach_a_template(field, path):
    """Neither copy has to handle these, because neither ever sees one."""
    settings = (
        {"firewall": {"allowed_paths": [path]}}
        if field == "firewall.allowed_paths"
        else {"rate_limit": {"rate": "1r/s", "paths": [path]}}
    )
    with pytest.raises(ValidationError):
        _site(**settings)


@pytest.mark.parametrize("field", ["firewall.allowed_paths", "rate_limit.paths"])
def test_a_pattern_over_the_limit_is_refused_for_both_rules(field):
    """The limit is measured on the Python side, so both sides must agree on it.

    The last list that fits renders with the same bytes on both sides.
    """
    paths = [f"/{index:03d}/" + "é" * 40 for index in range(100)]
    fits = next(
        paths[:count]
        for count in range(len(paths), 0, -1)
        if len(allowed_paths_pattern(paths[:count]).encode())
        <= ALLOWED_PATHS_PATTERN_LIMIT
    )
    over = paths[: len(fits) + 1]

    def settings(chosen):
        if field == "firewall.allowed_paths":
            return {"firewall": {"allowed_paths": chosen}}
        return {"rate_limit": {"rate": "1r/s", "paths": chosen}}

    site = _site(**settings(fits))
    rendered = (
        _access_rule(_fragment(ACCESS, site=site))
        if field == "firewall.allowed_paths"
        else _map_rule(_fragment(HTTP, blitzecdn_nginx_sites=[site]), "parity")
    )
    assert rendered is not None
    assert rendered[0].encode() == allowed_paths_pattern(fits).encode()
    with pytest.raises(ValidationError, match="too long together"):
        _site(**settings(over))


def test_every_rung_names_its_own_zone_on_both_sides():
    zones = {rate_limit_zone(rate) for rate in RATE_LIMIT_RATES}

    assert len(zones) == len(RATE_LIMIT_RATES)
    assert _zone_mismatches(RATE_LIMIT_RATES) == []


@settings(max_examples=60, deadline=None)
@given(rates=st.lists(st.sampled_from(RATE_LIMIT_RATES), min_size=1, max_size=8))
def test_generated_sites_declare_exactly_the_zones_they_read(rates):
    assert _zone_mismatches(rates) == []


@pytest.mark.parametrize(
    "rate",
    ["3r/s", "10r/h", "0r/s", "1000r/m", "10R/S", "1r/s;", "r/s", "", "1r/s/2"],
)
def test_rates_off_the_ladder_are_refused(rate):
    with pytest.raises(ValidationError):
        _site(rate_limit={"rate": rate})


# --- a drift is caught --------------------------------------------------------


def test_a_python_side_that_drops_the_prefix_rule_is_caught():
    def exact_only(paths: Sequence[str]) -> str:
        return "|".join(re.escape(path) + "$" for path in paths)

    problems = _parity(["/api/"], caseless=False, python=exact_only)

    assert any(
        problem.startswith("python: '/api/") and "should match" in problem
        for problem in problems
    ), problems
    # The templates still render the true rule, so the spellings differ too.
    assert any("python '/api/$'" in problem for problem in problems), problems


@pytest.mark.parametrize("name", [ACCESS, HTTP])
def test_a_template_that_drops_the_prefix_rule_is_caught(name):
    assert _PATH_ALTERNATIVE in _source(name)
    drifted = _source(name).replace(_PATH_ALTERNATIVE, "(path | regex_escape) ~ '$'")

    problems = _parity(
        ["/api/"], caseless=False, **{"access" if name == ACCESS else "http": drifted}
    )

    assert any(problem.startswith(f"{name}: ") for problem in problems), problems


@pytest.mark.parametrize("name", [ACCESS, HTTP])
def test_a_template_that_names_zones_differently_is_caught(name):
    original = "blitzecdn_rate_{{ rate_limit.rate | replace('/', '') }}"
    if name == HTTP:
        original = "blitzecdn_rate_{{ rate | replace('/', '') }}"
    assert original in _source(name)
    drifted = _source(name).replace(original, original.replace("''", "'_'"))

    problems = _zone_mismatches(
        RATE_LIMIT_RATES, **{"access" if name == ACCESS else "http": drifted}
    )

    assert problems, "a renamed zone went unnoticed"
