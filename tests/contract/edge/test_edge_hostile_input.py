"""Whatever a site's free-text settings accept, the configuration keeps its shape.

Generated text full of nginx syntax goes into each setting that is rendered
into a site's configuration. A value the policy refuses is fine. A value it
accepts must leave the rendered configuration with the same statements, at the
same depths, as an ordinary value does: anything else means the value closed a
quote, ended a statement or opened a block, which is how an operator's string
becomes an nginx directive.

`nginx -t` is not available here, so the files are read with nginx's own
tokenising rules instead, which is what decides where a statement ends.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import jinja2
import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from support.edge_render import (
    _capability_defaults,
    nginx_environment,
    nginx_resources,
    role_defaults,
)
from support.policy import site_from_settings

from blitzecdn.capabilities.sites.adapters.ansible import site_to_ansible

#: Characters with a meaning to nginx's lexer or to the regular expressions and
#: variables it evaluates, and a few ordinary ones to carry them.
_SYNTAX = "az09.-_/*~%$#;{}'\"\\ \t\n?&=:()[]^|@!,<>"
_TEXT = st.text(alphabet=st.sampled_from(_SYNTAX), min_size=1, max_size=24)
#: As generated, as a path, as a hostname and as an extension: most settings
#: refuse anything not starting like one, and those are the interesting values.
_VALUES = st.one_of(
    _TEXT,
    _TEXT.map(lambda text: f"/{text}"),
    _TEXT.map(lambda text: f"{text}.example.com"),
    _TEXT.map(lambda text: f".{text}"),
)


def _spliced(ordinary: str) -> st.SearchStrategy[str]:
    """The ordinary value with a little nginx syntax spliced in somewhere.

    Text that is all syntax is nearly always refused outright; this is what
    reaches the template often enough to be worth checking.
    """
    syntax = st.text(alphabet=st.sampled_from(_SYNTAX), min_size=1, max_size=4)
    return st.tuples(st.integers(0, len(ordinary)), syntax).map(
        lambda cut: ordinary[: cut[0]] + cut[1] + ordinary[cut[0] :]
    )


#: Each rendered free-text setting: how a value is placed in a site's settings,
#: and an ordinary value whose configuration the others are compared with.
FIELDS: dict[str, tuple[Callable[[str], dict[str, Any]], str]] = {
    "origin_host": (lambda value: {"origin_host": value}, "origin.example.com"),
    "origin_request_host": (
        lambda value: {"origin_request_host": value},
        "origin.example.com",
    ),
    "origin_sni": (lambda value: {"origin_sni": value}, "origin.example.com"),
    "server_names": (lambda value: {"server_names": [value]}, "cdn.example.com"),
    "firewall.allow_sources": (
        lambda value: {"firewall": {"allow_sources": [value]}},
        "203.0.113.0/24",
    ),
    "firewall.deny_sources": (
        lambda value: {"firewall": {"deny_sources": [value]}},
        "203.0.113.0/24",
    ),
    "firewall.denied_paths": (
        lambda value: {"firewall": {"denied_paths": [value]}},
        "/admin",
    ),
    "firewall.allowed_paths": (
        lambda value: {"firewall": {"allowed_paths": [value]}},
        "/admin",
    ),
    "rate_limit.paths": (
        lambda value: {"rate_limit": {"rate": "1r/s", "paths": [value]}},
        "/login",
    ),
    "cache_bypass_cookies": (
        lambda value: {"cache_bypass_cookies": [value]},
        "session",
    ),
    "cache_ignore_origin_headers": (
        lambda value: {"cache_ignore_origin_headers": [value]},
        ".css",
    ),
    "cache_ttl_overrides": (
        lambda value: {"cache_ttl_overrides": {value: "1d"}},
        ".css",
    ),
    "cache_valid_success": (lambda value: {"cache_valid_success": value}, "10m"),
}

#: Blocks whose statements are data, not directives: their first word is a
#: key, which a setting may well supply.
_DATA_BLOCKS = {"map", "geo"}


class NginxSyntaxError(AssertionError):
    pass


def _word_end(text: str, index: int) -> int:
    """Where the word starting at ``index`` ends, quoted or not."""
    if text[index] in "\"'":
        end = index + 1
        while end < len(text) and text[end] != text[index]:
            end += 2 if text[end] == "\\" else 1
        if end >= len(text):
            raise NginxSyntaxError(f"unterminated quote at {index}")
        return end + 1
    end = index
    while end < len(text) and not text[end].isspace() and text[end] not in ";{}":
        if text[end] == "\\":
            end += 2
        elif text.startswith("${", end):
            close = text.find("}", end)
            if close < 0:
                raise NginxSyntaxError(f"unterminated variable at {end}")
            end = close + 1
        else:
            end += 1
    return end


def _statements(text: str) -> list[tuple[int, list[str], str]]:
    """Each statement's depth, words and terminator, as nginx's lexer splits them.

    A quoted word runs to the matching quote, with backslash escapes; an
    unquoted one to whitespace, `;`, `{` or `}`, keeping backslash escapes and
    `${...}` inside it. `#` where a word would start begins a comment.
    """
    found: list[tuple[int, list[str], str]] = []
    words: list[str] = []
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
        elif char == "#":
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif char in ";{":
            if not words:
                raise NginxSyntaxError(f"empty statement at {index}")
            found.append((depth, words, char))
            words = []
            depth += char == "{"
            index += 1
        elif char == "}":
            if words or not depth:
                raise NginxSyntaxError(f"unexpected '}}' at {index}")
            depth -= 1
            index += 1
        else:
            end = _word_end(text, index)
            words.append(text[index:end])
            index = end
    if words or depth:
        raise NginxSyntaxError("the file ends inside a statement or block")
    return found


def _shape(text: str) -> list[tuple[int, str]]:
    """Each directive's depth and name; inside data blocks, only that one is there."""
    shape: list[tuple[int, str]] = []
    data_depth: int | None = None
    for depth, words, terminator in _statements(text):
        if data_depth is not None and depth <= data_depth:
            data_depth = None
        if data_depth is None:
            shape.append((depth, words[0]))
            if terminator == "{" and words[0] in _DATA_BLOCKS:
                data_depth = depth
        else:
            shape.append((depth, "<entry>"))
    return shape


@cache
def _renderer() -> tuple[jinja2.Template, dict[str, Any], list[jinja2.Template]]:
    """The site template, its context and the http-level fragments, built once.

    ``render_site`` rebuilds all three per call, which is fine for one site and
    far too slow for a few hundred generated ones.
    """
    environment = nginx_environment()
    resources = nginx_resources()
    context = role_defaults() | _capability_defaults()
    context["blitzecdn_nginx_resources"] = resources
    fragments = [
        environment.from_string(Path(resource["template"]).read_text(encoding="utf-8"))
        for resource in resources["http"]
    ]
    return environment.get_template("site.conf.j2"), context, fragments


def _render(field: str, value: str) -> str | None:
    """The site's configuration and its http-level fragments; None when refused."""
    place, _ = FIELDS[field]
    try:
        site = site_to_ansible(
            site_from_settings(
                {
                    "name": "hostile",
                    "server_names": ["hostile.example.com"],
                    "origin_host": "origin.example.com",
                }
                | place(value)
            )
        )
    except ValidationError, ValueError:
        return None
    template, context, fragments = _renderer()
    return template.render({**context, "item": site}) + "".join(
        fragment.render({**context, "blitzecdn_nginx_sites": [site]})
        for fragment in fragments
    )


def test_the_shape_reader_follows_nginx_tokenising():
    assert _shape("a \"b;c\" {\n  d 'e}' f\\;g;\n  # h; }\n  i ${j}k;\n}\n") == [
        (0, "a"),
        (1, "d"),
        (1, "i"),
    ]
    assert _shape("map $a $b {\n  default 0;\n  ~x;y 1;\n}\nz;\n") == [
        (0, "map"),
        (1, "<entry>"),
        (1, "<entry>"),
        (1, "<entry>"),
        (0, "z"),
    ]
    for broken in ('a "b;', "a {", "a; }", "a", "; a;"):
        with pytest.raises(NginxSyntaxError):
            _statements(broken)


@pytest.mark.parametrize("field", sorted(FIELDS))
def test_the_ordinary_value_is_rendered(field):
    """Without this, a field whose ordinary value is refused checks nothing."""
    _, ordinary = FIELDS[field]
    rendered = _render(field, ordinary)
    assert rendered is not None
    assert ordinary in rendered or field == "cache_valid_success"


@pytest.mark.parametrize("field", sorted(FIELDS))
@settings(max_examples=80, deadline=None)
@given(data=st.data())
def test_an_accepted_value_never_changes_the_configuration_shape(field, data):
    _, ordinary = FIELDS[field]
    value = data.draw(st.one_of(_VALUES, _spliced(ordinary)), label="value")
    rendered = _render(field, value)
    event("accepted" if rendered is not None else "refused")
    if rendered is None:
        return
    expected = _render(field, ordinary)
    assert expected is not None
    assert _shape(rendered) == _shape(expected)
