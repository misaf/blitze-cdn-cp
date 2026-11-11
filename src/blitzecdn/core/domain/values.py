"""Values that reach a rendered nginx configuration, each parsed in one place.

Every string a contract hands to an edge template is one of these types, or a
capability's own type built the same way. Each is an ``Annotated[str, ...]``
whose :class:`NginxValue` is both the parser and the marker: it normalises the
value, refuses what its kind does not allow and then, for anything rendered
into a directive, refuses every character that could end an nginx token, a
quoted string or a block. A parser that forgets a case cannot let ``;`` or a
newline through, and ``tests/architecture`` fails on a ``str`` field a template
reads that is not one of these.

Rendering is one function per context: :func:`bare`, :func:`quoted` and
:func:`regex`. Templates use Ansible's ``regex_escape``, which is
:func:`re.escape` and so :func:`regex`; the Python side never escapes by hand.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

from blitzecdn.core.domain.validation import (
    DURATION,
    HOST_NAME,
    SITE_NAME,
    hostname,
    ip_literal,
    ip_network,
)

__all__ = [
    "HTML_LIMIT",
    "UNSAFE_CHARACTERS",
    "Duration",
    "Hostname",
    "HtmlDocument",
    "IpAddress",
    "IpNetwork",
    "IpNetworkWidened",
    "NginxValue",
    "RuleName",
    "UrlPath",
    "VirtualHostName",
    "WildcardHostname",
    "bare",
    "quoted",
    "regex",
    "url_path",
]

#: Characters no value rendered into a directive may hold, whatever its kind.
#: Whitespace and ``;`` end a token or a directive, braces open or close a
#: block, quotes and the backslash end or escape a quoted string, ``#`` starts
#: a comment and the backtick is refused for the shell-quoting scripts that read
#: the same document. Control characters are refused separately.
UNSAFE_CHARACTERS = frozenset(" \t\r\n\f\v;{}\"'\\`#")


def _refuse_unsafe(value: str, kind: str) -> str:
    unsafe = sorted(
        {character for character in value if character in UNSAFE_CHARACTERS}
    )
    if unsafe:
        raise ValueError(
            f"{kind} {value!r} may not contain whitespace, quotes, backslashes, "
            "backticks, '#', ';', '{' or '}'"
        )
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError(f"{kind} {value!r} may not contain control characters")
    return value


@dataclass(frozen=True, slots=True)
class NginxValue:
    """The parser for one kind of value, and the mark that a field has one.

    ``parse`` normalises a value or raises ``ValueError``. ``directive`` is
    false only for a value written whole as a file's body (an HTML page), never
    spliced into configuration; such a value is not held to
    :data:`UNSAFE_CHARACTERS`.
    """

    kind: str
    parse: Callable[[str], str]
    directive: bool = True

    def validate(self, value: str) -> str:
        parsed = self.parse(value)
        return _refuse_unsafe(parsed, self.kind) if self.directive else parsed

    def __get_pydantic_core_schema__(
        self, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            self.validate, handler(source)
        )


# --- rendering ----------------------------------------------------------------


def bare(value: str) -> str:
    """``value`` as an unquoted nginx token."""
    return _refuse_unsafe(value, "value")


def quoted(value: str) -> str:
    """``value`` as a double-quoted nginx string."""
    return f'"{_refuse_unsafe(value, "value")}"'


def regex(value: str) -> str:
    """``value`` matched literally inside an nginx regex.

    The same escape as the templates' ``regex_escape``, which is ``re.escape``.
    """
    return re.escape(_refuse_unsafe(value, "value"))


# --- names --------------------------------------------------------------------


def _virtual_host_name(value: str) -> str:
    normalized = value.strip().lower()
    if not HOST_NAME.fullmatch(normalized):
        raise ValueError(
            "name must contain only a-z, 0-9, and hyphens, and start with "
            "a letter or a digit"
        )
    return normalized


def _rule_name(value: str) -> str:
    normalized = value.strip().lower()
    if not SITE_NAME.fullmatch(normalized):
        raise ValueError(
            "name must start with a letter and contain only a-z, 0-9, and hyphens"
        )
    return normalized


#: A DNS hostname or IP literal, lower case, no trailing dot.
Hostname = Annotated[str, NginxValue("hostname", hostname)]

#: A :data:`Hostname`, or ``*.`` followed by one.
WildcardHostname = Annotated[
    str, NginxValue("hostname", lambda value: hostname(value, wildcard=True))
]

#: A derived virtual host name; it names nginx variables, upstreams and files.
VirtualHostName = Annotated[str, NginxValue("host name", _virtual_host_name)]

#: A rule's name; it names an nginx named location.
RuleName = Annotated[str, NginxValue("rule name", _rule_name)]


# --- addresses ----------------------------------------------------------------
#
# Each strips, refuses an IPv6 zone index and stores the canonical spelling
# (`2001:db8::1`, `192.0.2.0/24`), so equal values compare equal and nothing
# after a `%` ever reaches nginx or the host firewall. Strings rather than
# `ipaddress` objects: templates and JSON read them as text.


def _address_text(value: str) -> str:
    candidate = value.strip()
    try:
        return str(ip_literal(candidate))
    except ValueError as error:
        raise ValueError(f"{value!r} must be an IP address: {error}") from error


def _exact_network_text(value: str) -> str:
    candidate = value.strip()
    try:
        return str(ip_network(candidate, strict=True))
    except ValueError as error:
        raise ValueError(
            f"{value!r} is not an IP address or CIDR network: {error}"
        ) from error


def _widened_network_text(value: str) -> str:
    candidate = value.strip()
    try:
        return str(ip_network(candidate, strict=False))
    except ValueError as error:
        raise ValueError(
            f"{value!r} is not a CIDR network such as '203.0.113.0/24': {error}"
        ) from error


#: One IPv4 or IPv6 address.
IpAddress = Annotated[str, NginxValue("IP address", _address_text)]

#: An address or CIDR network with no host bits set; a bare address is its /32
#: or /128. ``192.0.2.8/24`` is refused, as it is ambiguous.
IpNetwork = Annotated[str, NginxValue("network", _exact_network_text)]

#: A CIDR network whose host bits are masked off: ``192.0.2.8/24`` means
#: ``192.0.2.0/24``, an operator naming the network they are on.
IpNetworkWidened = Annotated[str, NginxValue("network", _widened_network_text)]


# --- paths and times ----------------------------------------------------------


def url_path(value: str) -> str:
    """A request path to match: absolute, at most 512 characters, no query.

    Matched against nginx's ``$uri``, so ``?`` and ``#`` are refused rather
    than ignored: ``$uri`` never contains them, and a path naming one would
    silently match nothing.
    """
    candidate = value.strip()
    if not candidate.startswith("/"):
        raise ValueError(f"path {value!r} must start with '/'")
    if len(candidate) > 512:
        raise ValueError("a path must be at most 512 characters")
    _refuse_unsafe(candidate, "path")
    if "?" in candidate or "#" in candidate:
        raise ValueError(
            f"path {value!r} may not contain '?' or '#': a rule matches the "
            "path alone, never the query string or fragment"
        )
    return candidate


def _duration(value: str) -> str:
    candidate = value.strip()
    if not DURATION.fullmatch(candidate):
        raise ValueError(
            "duration must be a non-negative integer followed by ms, s, m, h, d, or w"
        )
    return candidate


#: A request path, matched exactly or, ending in ``/``, as a prefix.
UrlPath = Annotated[str, NginxValue("path", url_path)]

#: An nginx time value: ``10m``, ``500ms``, ``0s``.
Duration = Annotated[str, NginxValue("duration", _duration)]


# --- documents ----------------------------------------------------------------

#: Bytes one HTML document may take, UTF-8 encoded. It ships inside every
#: configuration release and is served from the edge's own disk.
HTML_LIMIT = 64 * 1024


def _html_document(value: str) -> str:
    if not value.strip():
        raise ValueError("the page is empty; clear it instead")
    if "\x00" in value:
        raise ValueError("the page contains a NUL character")
    if len(value.encode("utf-8")) > HTML_LIMIT:
        raise ValueError(
            f"the page is larger than {HTML_LIMIT // 1024} KiB; inline only what "
            "it needs"
        )
    return value


#: An opaque HTML page, written whole as a file the edge serves. Never spliced
#: into a directive, so its content is the author's.
HtmlDocument = Annotated[
    str, NginxValue("HTML document", _html_document, directive=False)
]
