"""Path matches, and what a path-scoped rule does to the requests under one.

A path is matched against Nginx's ``$uri``: decoded, with dot segments resolved
and slashes merged, so ``/%61dmin/`` and ``//admin/`` are ``/admin/``. One that
ends in ``/`` is a prefix and any other is exact, the convention the firewall's
``allowed_paths`` already uses, so ``/static/`` covers ``/static/app.css`` and
``/robots.txt`` covers only itself.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from blitzecdn.core.domain.values import (
    NginxValue,
    RuleName,
    UrlPath,
    WildcardHostname,
    regex,
)

__all__ = [
    "PathLocation",
    "PathRedirect",
    "path_is_prefix",
    "path_matches",
    "path_pattern",
]

#: The capture a redirect target may name: what followed a prefix path.
SUFFIX = "$1"

#: An absolute http(s) URL or a path on the same host.
_TARGET = re.compile(r"^(?:https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?)?/\S*$")


def path_is_prefix(path: str) -> bool:
    """Whether ``path`` covers everything below it, or only itself."""
    return path.endswith("/")


def path_pattern(path: str) -> str:
    """The anchored regex the edge renders for ``path``.

    Mirrors ``site.conf.j2``, whose ``regex_escape`` is :func:`regex`.
    """
    return "^" + regex(path) + ("" if path_is_prefix(path) else "$")


def path_matches(path: str, candidate: str) -> bool:
    """Whether a normalized request path is covered by ``path``."""
    if path_is_prefix(path):
        return candidate.startswith(path)
    return candidate == path


def _redirect_target(value: str) -> str:
    """An absolute ``http(s)`` URL or a path, naming no ``$`` but :data:`SUFFIX`.

    Characters that could end the quoted string or directive it is rendered in
    are refused by :class:`~blitzecdn.core.domain.values.NginxValue`.
    """
    candidate = value.strip()
    if not _TARGET.fullmatch(candidate):
        raise ValueError(
            "a redirect target must be an absolute http:// or https:// URL "
            "or a path starting with '/'"
        )
    if "$" in candidate.replace(SUFFIX, ""):
        raise ValueError(
            f"a redirect target may name {SUFFIX}, what followed the "
            "matched prefix, and no other '$' variable"
        )
    if re.search(r"\$1[0-9]", candidate):
        raise ValueError(
            f"{SUFFIX} may not be followed by a digit, which Nginx would "
            "read as a different capture"
        )
    if "#" in candidate:
        raise ValueError("a redirect target may not carry a fragment")
    return candidate


#: Where a redirect sends a visitor; rendered into a quoted ``return``.
RedirectTarget = Annotated[str, NginxValue("redirect target", _redirect_target)]


class PathRedirect(BaseModel):
    """Answer every request under a path with a redirect, before the origin.

    ``target`` is an absolute ``http(s)`` URL or a path on the same host. On a
    prefix path it may name ``$1``, which is what followed the prefix, still
    percent-encoded as the visitor sent it: ``/old/`` to ``/new/$1`` sends
    ``/old/a/b`` to ``/new/a/b``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[301, 302, 307, 308] = 301
    target: RedirectTarget = Field(min_length=1, max_length=2048)
    #: Append the visitor's query string, so ``/old/?page=2`` keeps ``page=2``.
    preserve_query_string: bool = True

    @model_validator(mode="after")
    def validate_query_handling(self) -> Self:
        """A preserved query string is appended with ``?``, so the target has none."""
        if self.preserve_query_string and "?" in self.target:
            raise ValueError(
                "a redirect target with its own query string cannot also "
                "preserve the visitor's; set preserve_query_string to false, "
                "or drop the query from the target"
            )
        return self

    @property
    def uses_suffix(self) -> bool:
        return SUFFIX in self.target


class PathLocation(BaseModel):
    """One path rule as a virtual host serves it, on some of its hostnames.

    Either a redirect or a policy: the host's policy with the rule's path-scoped
    overrides applied. A virtual host renders its locations in this order, and
    the first whose path matches answers the request.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The rule this came from, which also names the location in Nginx.
    rule: RuleName
    path: UrlPath
    #: The host's hostnames this rule covers: a rule's ``match`` can be narrower
    #: than the hostnames the host groups together.
    server_names: tuple[WildcardHostname, ...] = Field(min_length=1)
    redirect: PathRedirect | None = None
    #: Only the path-scoped settings; every other one is the host's.
    settings: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if (self.redirect is not None) != bool(self.settings):
            return self
        raise ValueError(
            "a path location either redirects or carries settings, not both"
        )

    @property
    def exact(self) -> bool:
        return not path_is_prefix(self.path)
