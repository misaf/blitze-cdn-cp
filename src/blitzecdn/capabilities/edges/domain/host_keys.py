"""An edge's SSH host key, and which of the keys a scan found an operator trusts.

The controller connects with ``StrictHostKeyChecking`` and ``BatchMode``, so a
key has to be recorded before the first deploy. Recording one is a decision:
either the operator compares the fingerprint with the host's own console, or
they pass the fingerprint they already have and only a key matching it is kept.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, field_validator

from blitzecdn.core.exceptions import ConfigurationError

#: Addresses the control-plane role records itself, in a file of their own
#: that the controller's SSH configuration names for these two hosts only.
#: Enrolling them in the edge file would be read by nothing.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})

_PREFIX = "SHA256:"


class HostKey(BaseModel):
    """One public host key, as ``ssh-keyscan`` prints it and known_hosts holds it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: ``ssh-ed25519``, ``ecdsa-sha2-nistp256``, ``ssh-rsa``…
    key_type: str
    #: The base64 key blob.
    key: str

    @field_validator("key_type", "key")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if not value or any(character.isspace() for character in value):
            raise ValueError("cannot be empty or contain whitespace")
        return value

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("host key is not base64") from error
        return value

    @property
    def fingerprint(self) -> str:
        """``SHA256:…``, as ``ssh-keygen -lf`` and the SSH client print it."""
        digest = hashlib.sha256(base64.b64decode(self.key)).digest()
        return _PREFIX + base64.b64encode(digest).decode("ascii").rstrip("=")


def known_hosts_pattern(host: str, port: int) -> str:
    """The name SSH looks a host up by: bare on port 22, ``[host]:port`` otherwise."""
    return host if port == 22 else f"[{host}]:{port}"


def normalize_fingerprint(value: str) -> str:
    """Accept ``SHA256:abc`` or ``abc``; refuse MD5 and anything that is not one."""
    candidate = value.strip()
    if candidate.upper().startswith("MD5:") or candidate.count(":") > 1:
        raise ConfigurationError(
            f"{value!r} is an MD5 fingerprint; give the SHA256 one that "
            "'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub' prints on the edge"
        )
    if candidate.startswith(_PREFIX):
        candidate = candidate[len(_PREFIX) :]
    try:
        digest = base64.b64decode(
            candidate + "=" * (-len(candidate) % 4), validate=True
        )
    except (binascii.Error, ValueError):
        digest = b""
    if len(digest) != hashlib.sha256().digest_size:
        raise ConfigurationError(
            f"{value!r} is not a SHA256 host key fingerprint such as "
            "'SHA256:uNiVztksCsDhcc0u9e8BujQXVUpKZIDTMczCvj3tD2s'"
        )
    return _PREFIX + candidate.rstrip("=")


def select_host_keys(
    scanned: Iterable[HostKey], fingerprints: Iterable[str]
) -> tuple[HostKey, ...]:
    """The scanned keys the operator vouched for; at least one must match.

    Only the matching keys are kept. Another key the host offered is not
    vouched for, and recording it would let it stand in for the one that is.
    """
    wanted = {normalize_fingerprint(value) for value in fingerprints}
    offered = tuple(scanned)
    chosen = tuple(key for key in offered if key.fingerprint in wanted)
    if not chosen:
        seen = ", ".join(f"{key.key_type} {key.fingerprint}" for key in offered)
        raise ConfigurationError(
            "no host key the edge offered matches the given fingerprint "
            f"(offered: {seen or 'none'}). Check the fingerprint on the host "
            "itself; a mismatch can mean the connection is being intercepted."
        )
    return chosen
