"""MaxMind's database download endpoint, called by the controller alone."""

from __future__ import annotations

import base64
import io
import tarfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from pydantic import SecretStr

from blitzecdn.capabilities.security.geoip.domain import (
    EDITION,
    MAXIMUM_DATABASE_BYTES,
)
from blitzecdn.core.exceptions import ConfigurationError, ExecutionError

#: MaxMind's documented direct-download endpoint. It answers with a redirect to
#: a presigned object URL, which must not receive the account's credentials.
DOWNLOAD_URL = "https://download.maxmind.com/geoip/databases/{edition}/download"

#: Compressed, the archive is a fraction of the database it holds.
_MAXIMUM_ARCHIVE_BYTES = MAXIMUM_DATABASE_BYTES


class MaxMindDownload:
    """Fetch the current GeoLite2 archive and return the database inside it."""

    def __init__(
        self,
        *,
        account_id: SecretStr,
        license_key: SecretStr,
        edition: str = EDITION,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._account_id = account_id
        self._license_key = license_key
        self._edition = edition
        self._timeout = timeout_seconds

    def download(self) -> bytes:
        return self._extract(self._fetch())

    def _fetch(self) -> bytes:
        url = (
            DOWNLOAD_URL.format(edition=quote(self._edition, safe=""))
            + "?suffix=tar.gz"
        )
        request = Request(url, method="GET")  # noqa: S310  # nosec B310 - fixed https URL
        token = base64.b64encode(
            (
                f"{self._account_id.get_secret_value()}:"
                f"{self._license_key.get_secret_value()}"
            ).encode()
        ).decode("ascii")
        # Unredirected: the presigned URL MaxMind redirects to rejects a second
        # authorization scheme, and has no business seeing the key anyway.
        request.add_unredirected_header("Authorization", f"Basic {token}")
        request.add_header("User-Agent", "blitzecdn-geoip")
        try:
            with urlopen(request, timeout=self._timeout) as response:  # noqa: S310  # nosec B310
                payload: bytes = response.read(_MAXIMUM_ARCHIVE_BYTES + 1)
        except HTTPError as exc:
            # The error carries the response body; release it before raising.
            exc.close()
            if exc.code in (401, 403):
                raise ConfigurationError(
                    "MaxMind refused BLITZE_MAXMIND_ACCOUNT_ID and "
                    f"BLITZE_MAXMIND_LICENSE_KEY (HTTP {exc.code}); check both "
                    "in the controller's .env"
                ) from exc
            if exc.code == 429:
                raise ExecutionError(
                    "MaxMind's daily download limit for this account is spent; "
                    "the next scheduled refresh will try again"
                ) from exc
            raise ExecutionError(
                f"MaxMind answered HTTP {exc.code} for {self._edition}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ExecutionError(f"could not reach MaxMind: {exc}") from exc
        if len(payload) > _MAXIMUM_ARCHIVE_BYTES:
            raise ExecutionError("the MaxMind archive exceeds 128 MiB")
        return payload

    def _extract(self, archive: bytes) -> bytes:
        """The one ``<edition>.mmdb`` member, read without extracting to disk."""
        wanted = f"{self._edition}.mmdb"
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
                for member in tar:
                    if not member.isreg() or member.name.rsplit("/", 1)[-1] != wanted:
                        continue
                    if member.size > MAXIMUM_DATABASE_BYTES:
                        raise ExecutionError(f"{wanted} exceeds 128 MiB")
                    stream = tar.extractfile(member)
                    if (
                        stream is None
                    ):  # pragma: no cover - regular files always have one
                        break
                    return stream.read()
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise ExecutionError(f"the MaxMind archive is unreadable: {exc}") from exc
        raise ExecutionError(f"the MaxMind archive holds no {wanted}")
