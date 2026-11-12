"""Encrypt archives to age recipients, interoperable with the ``age`` tool.

An encrypted archive is a plain ``age`` file wrapping the usual gzipped tar,
so ``age -d -i KEY ARCHIVE | tar -xz`` opens one where BlitzeCDN won't start.
The controller holds recipients (public keys) only.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, BinaryIO

import pyrage
from pyrage import ssh, x25519

from blitzecdn.capabilities.backup.adapters.archive import publish_new
from blitzecdn.core.exceptions import ConfigurationError

#: Every age file begins with one of these: binary, or ASCII-armored.
_HEADERS = (b"age-encryption.org/", b"-----BEGIN AGE ENCRYPTED FILE-----")

#: The SSH private key encodings ``age`` reads: OpenSSH's own, and the PEM
#: form ``ssh-keygen -m PEM`` writes for RSA.
_SSH_KEYS = (
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
)


def parse_recipient(text: str) -> Any:
    """One recipient: an ``age1…`` public key or an ``ssh-ed25519``/``ssh-rsa`` key.

    Raises ``ValueError``, so a configuration typo fails validation at startup
    rather than the next scheduled backup.
    """
    value = text.strip()
    try:
        if value.startswith("age1"):
            return x25519.Recipient.from_str(value)
        if value.startswith(("ssh-ed25519 ", "ssh-rsa ")):
            return ssh.Recipient.from_str(value)
    except pyrage.RecipientError as exc:
        raise ValueError(f"backup recipient {value!r} is not a valid key") from exc
    raise ValueError(
        f"backup recipient {value!r} is neither an age public key (age1...) "
        "nor an ssh-ed25519 or ssh-rsa public key"
    )


def parse_identities(text: str) -> list[Any]:
    """The keys in an identity file: age secret keys, or one SSH private key.

    Never echoes the input: it is a private key.
    """
    if any(header in text for header in _SSH_KEYS):
        try:
            return [ssh.Identity.from_buffer(text.encode("utf-8"))]
        except pyrage.IdentityError as exc:
            raise ConfigurationError(
                "the identity is not a usable SSH private key; a "
                "passphrase-protected key must be decrypted first"
            ) from exc
    keys = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not keys:
        raise ConfigurationError("the identity file holds no key")
    try:
        return [x25519.Identity.from_str(key) for key in keys]
    except pyrage.IdentityError as exc:
        raise ConfigurationError(
            "the identity is neither an age secret key (AGE-SECRET-KEY-1...) "
            "nor an unencrypted SSH private key"
        ) from exc


class AgeCipher:
    """Seal archives to the configured recipients; open them with a given identity."""

    def __init__(self, recipients: Sequence[str]) -> None:
        self._recipients = [parse_recipient(recipient) for recipient in recipients]

    @property
    def encrypting(self) -> bool:
        return bool(self._recipients)

    def encrypted(self, archive: Path) -> bool:
        try:
            with archive.open("rb") as stream:
                header = stream.read(max(len(prefix) for prefix in _HEADERS))
        except OSError:
            return False
        return header.startswith(_HEADERS)

    def encrypt(self, source: Path, destination: Path) -> None:
        if not self._recipients:
            raise ConfigurationError("no backup recipients are configured")

        def seal(stream: BinaryIO) -> None:
            with source.open("rb") as plain:
                try:
                    pyrage.encrypt_io(plain, stream, self._recipients)
                except pyrage.EncryptError as exc:
                    raise ConfigurationError(
                        f"cannot encrypt the backup: {exc}"
                    ) from exc

        publish_new(destination, seal)

    def decrypt(self, source: Path, destination: Path, identity: str) -> None:
        identities = parse_identities(identity)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with source.open("rb") as sealed, os.fdopen(descriptor, "wb") as plain:
            try:
                pyrage.decrypt_io(sealed, plain, identities)
            except pyrage.DecryptError as exc:
                raise ConfigurationError(f"cannot decrypt {source}: {exc}") from exc
            except OSError as exc:
                # The age payload is authenticated chunk by chunk; a truncated
                # or altered archive surfaces here, after some plaintext.
                raise ConfigurationError(
                    f"{source} is damaged or was altered; it failed authentication"
                ) from exc


__all__ = ["AgeCipher", "parse_identities", "parse_recipient"]
