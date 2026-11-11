"""Every string an edge template reads is a typed nginx value.

A contract field that reaches configuration is parsed by one
:class:`~blitzecdn.core.domain.values.NginxValue`, which refuses whatever could
end a token, a quoted string or a block. A bare ``str`` field is a validator
someone has to remember to write, which is how the audit's gaps happened.
"""

from __future__ import annotations

import re
import types
import typing
from collections.abc import Iterator
from enum import Enum
from typing import Annotated, Any, Literal, get_args, get_origin

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError
from support.paths import SOURCE

from blitzecdn.core.domain.paths import PathLocation, path_pattern
from blitzecdn.core.domain.site import CdnSite
from blitzecdn.core.domain.site_policy import SitePolicy
from blitzecdn.core.domain.values import (
    UNSAFE_CHARACTERS,
    Duration,
    Hostname,
    HtmlDocument,
    IpNetwork,
    NginxValue,
    UrlPath,
    WildcardHostname,
    bare,
    quoted,
    regex,
)

#: String fields a template reads that are not configuration values, with why.
_EXEMPT = {
    # Keys are setting names, refused by `CdnSite` unless they are path-scoped
    # fields of the composed policy; the values are validated by those fields.
    (PathLocation, "settings"),
}


def _template_words() -> frozenset[str]:
    words: set[str] = set()
    for template in SOURCE.rglob("*.j2"):
        words.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", template.read_text()))
    return frozenset(words)


def _string_leaves(annotation: Any, marked: bool = False) -> Iterator[bool]:
    """Whether each ``str`` inside ``annotation`` carries an :class:`NginxValue`."""
    origin = get_origin(annotation)
    if origin is Annotated:
        inner, *metadata = get_args(annotation)
        yield from _string_leaves(
            inner, marked or any(isinstance(m, NginxValue) for m in metadata)
        )
    elif origin in (typing.Union, types.UnionType, tuple, list, dict, frozenset):
        for argument in get_args(annotation):
            if argument is not Ellipsis:
                yield from _string_leaves(argument, marked)
    elif origin is Literal or (
        isinstance(annotation, type) and issubclass(annotation, Enum)
    ):
        return  # A closed vocabulary the contract spells itself.
    elif annotation is str:
        yield marked


def _models() -> Iterator[type[BaseModel]]:
    pending: list[type[BaseModel]] = [CdnSite, SitePolicy.composed()]
    seen: set[type[BaseModel]] = set()
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        yield model
        for info in model.model_fields.values():
            pending.extend(_nested_models(info.annotation))


def _nested_models(annotation: Any) -> Iterator[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
    for argument in get_args(annotation):
        yield from _nested_models(argument)


def test_every_string_a_template_reads_is_a_typed_nginx_value():
    words = _template_words()
    offenders = []
    for model in _models():
        for name, info in model.model_fields.items():
            if name not in words or (model, name) in _EXEMPT:
                continue
            marked = any(isinstance(m, NginxValue) for m in info.metadata)
            leaves = list(_string_leaves(info.annotation, marked))
            if not all(leaves):
                offenders.append(f"{model.__qualname__}.{name}: {info.annotation}")
    assert offenders == []


def test_the_walk_reaches_the_contributed_contracts():
    """Guards the test above against passing because it saw nothing."""
    names = {model.__name__ for model in _models()}
    assert {"CdnSite", "PathLocation", "SiteFirewall", "CachePolicy"} <= names


# --- the values themselves ----------------------------------------------------

_HOSTILE = [f"/a{character}b" for character in sorted(UNSAFE_CHARACTERS)] + [
    "/a\x00b",
    "/a\x7fb",
]


@pytest.mark.parametrize("value", _HOSTILE)
def test_a_path_refuses_what_ends_an_nginx_token(value):
    with pytest.raises(ValidationError):
        TypeAdapter(UrlPath).validate_python(value)


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (Hostname, "fe80::1%x;return"),
        (WildcardHostname, "*.example.com;"),
        (IpNetwork, "fe80::/64%eth0"),
        (Duration, "10m;"),
    ],
)
def test_each_kind_refuses_a_directive_break(kind, value):
    with pytest.raises(ValidationError):
        TypeAdapter(kind).validate_python(value)


def test_values_are_normalised_once():
    assert TypeAdapter(Hostname).validate_python(" Example.COM. ") == "example.com"
    assert TypeAdapter(IpNetwork).validate_python("2001:DB8::/32") == "2001:db8::/32"


def test_an_html_document_is_a_file_body_not_a_token():
    page = '<p style="color: red">Back soon; {really}</p>'
    assert TypeAdapter(HtmlDocument).validate_python(page) == page


def test_rendering_contexts():
    assert bare("example.com") == "example.com"
    assert quoted("/a/b") == '"/a/b"'
    assert regex("/a.b") == re.escape("/a.b")
    assert path_pattern("/a.b") == "^/a\\.b$"
    for render in (bare, quoted, regex):
        with pytest.raises(ValueError, match="may not contain"):
            render('a"b')
