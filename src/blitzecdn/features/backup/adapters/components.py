"""What each backup component actually copies, and how it puts it back.

Every component answers the same four questions — is there anything here, take
a copy, is this copy sound, put it back — and each answers them differently
enough that a single generic "copy these paths" adapter would be a lie. The
database cannot be copied with `cp`; the ACME tree is held together by
symlinks; the configuration is four files in three places.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tomllib
from pathlib import Path
from shutil import rmtree
from uuid import uuid4

from sqlalchemy import create_engine

from blitzecdn.core.config import (
    MACHINE_SPECIFIC_CONFIG_KEYS,
    PORTABLE_CONFIG_KEYS,
    PORTABLE_ENVIRONMENT_KEYS,
    Settings,
)
from blitzecdn.core.exceptions import ConfigurationError
from blitzecdn.core.filesystem import atomic_write_bytes
from blitzecdn.features.backup.domain import BackupComponent

#: The archived database's name inside the archive. Fixed rather than derived
#: from `database_path`, because the restoring host may have configured a
#: different one and the archive must not depend on either.
DATABASE_MEMBER = "blitzecdn.db"

#: Where a component records the symlinks it could not store as symlinks.
#: Restoring recreates them; see `_LinkedTree`.
LINKS_MEMBER = ".links.json"

_SQLITE_MAGIC = b"SQLite format 3\x00"
_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")


def _copy_tree(source: Path, destination: Path, *, links: dict[str, str]) -> None:
    """Copy a directory, recording symlinks instead of following them.

    Certbot's configuration directory is a tree of symlinks by design —
    `live/<name>/fullchain.pem` points into `archive/` — so dereferencing would
    produce a tree certbot refuses to renew from, while storing the links as
    tar links would put a link in an archive this project will not extract.
    They are recorded in a sidecar and recreated on restore, where each target
    is checked to stay inside the component before anything is created.
    """
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        target = destination / relative
        if path.is_symlink():
            links[relative] = os.readlink(path)
            continue
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            continue
        if not path.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_bytes(target, path.read_bytes())


def _restore_links(root: Path, links: dict[str, str]) -> None:
    """Recreate recorded symlinks, refusing any that leaves the component."""
    for relative, target in sorted(links.items()):
        link = root / relative
        resolved = (link.parent / target).resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise ConfigurationError(
                f"backup contains a link that escapes its component: {relative}"
            )
        link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        link.unlink(missing_ok=True)
        link.symlink_to(target)


def _read_links(staging: Path) -> dict[str, str]:
    path = staging / LINKS_MEMBER
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"{LINKS_MEMBER} is not valid JSON") from exc
    if not isinstance(document, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in document.items()
    ):
        raise ConfigurationError(f"{LINKS_MEMBER} is not a mapping of link to target")
    root = staging.resolve()
    for relative, target in document.items():
        link = staging / relative
        if (
            not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not link.resolve().is_relative_to(root)
        ):
            raise ConfigurationError(
                f"backup contains an invalid link path: {relative}"
            )
        if (link.parent / target).resolve().is_relative_to(root) is False:
            raise ConfigurationError(
                f"backup contains a link that escapes its component: {relative}"
            )
        if link.exists() or link.is_symlink():
            raise ConfigurationError(
                f"backup records a link over an existing member: {relative}"
            )
    return document


def _toml_document(payload: bytes) -> dict[str, object]:
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError("config/blitzecdn.toml is not valid TOML") from exc
    section = document.get("blitzecdn", {})
    if not isinstance(section, dict):
        raise ConfigurationError("config/blitzecdn.toml has no [blitzecdn] table")
    return section


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    raise ConfigurationError("config/blitzecdn.toml contains an unsupported value")


def _render_toml(values: dict[str, object]) -> bytes:
    lines = ["[blitzecdn]", ""]
    lines.extend(f"{key} = {_toml_value(values[key])}" for key in sorted(values))
    return ("\n".join(lines) + "\n").encode()


def _environment_assignments(payload: bytes) -> dict[str, str]:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ConfigurationError("config/env is not valid UTF-8") from exc
    assignments: dict[str, str] = {}
    for line in lines:
        match = _ASSIGNMENT.match(line.strip())
        if match:
            assignments[match.group(1)] = line
    return assignments


def _render_environment(assignments: dict[str, str]) -> bytes:
    if not assignments:
        return b""
    return (
        "\n".join(assignments[name] for name in sorted(assignments)) + "\n"
    ).encode()


class DatabaseComponent:
    """The control-plane database, copied the only way that is consistent.

    `VACUUM INTO` rather than a file copy. The database runs in WAL mode, so
    recent commits live in the `-wal` beside the `.db` and copying one without
    the other is a backup of some earlier moment that does not say so. This
    asks SQLite to write a complete database, which is safe against a
    controller that is still serving and produces one file rather than three.
    """

    component = BackupComponent.DATABASE
    #: The services hold the database open and cache nothing across a restart,
    #: but they do keep an open handle to the file being replaced.
    requires_offline = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def _path(self) -> Path:
        return self._settings.database_path

    def present(self) -> bool:
        return self._path.is_file()

    def export(self, staging: Path) -> None:
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = staging / DATABASE_MEMBER
        engine = create_engine(f"sqlite+pysqlite:///{self._path}")
        try:
            with engine.connect() as connection:
                # Bound as a parameter rather than interpolated: a path is
                # operator input, and this one reaches SQL.
                connection.exec_driver_sql("VACUUM INTO ?", (str(target),))
        finally:
            engine.dispose()
        target.chmod(0o600)

    def validate(self, staging: Path) -> None:
        path = staging / DATABASE_MEMBER
        if not path.is_file():
            raise ConfigurationError(
                f"backup declares a database but has no {self.component.value}/"
                f"{DATABASE_MEMBER}"
            )
        with path.open("rb") as stream:
            if stream.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
                raise ConfigurationError(
                    f"{self.component.value}/{DATABASE_MEMBER} is not a SQLite database"
                )
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise ConfigurationError(
                f"{self.component.value}/{DATABASE_MEMBER} cannot be opened: {exc}"
            ) from exc
        try:
            outcome = connection.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as exc:
            raise ConfigurationError(
                f"{self.component.value}/{DATABASE_MEMBER} is damaged: {exc}"
            ) from exc
        finally:
            connection.close()
        if not outcome or outcome[0] != "ok":
            raise ConfigurationError(
                f"{self.component.value}/{DATABASE_MEMBER} failed its integrity check"
            )

    def restore(self, staging: Path) -> None:
        """Install the archived database, then bring its schema up to date.

        The `-wal` and `-shm` beside the old database are removed rather than
        left: they belong to the file being replaced, and SQLite would apply
        them over the restored one — silently reintroducing exactly the state
        the restore was meant to discard.
        """
        source = staging / DATABASE_MEMBER
        destination = self._path
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_bytes(destination, source.read_bytes())
        for suffix in ("-wal", "-shm"):
            destination.with_name(destination.name + suffix).unlink(missing_ok=True)
        # Imported here rather than at module scope: this is the one place a
        # component reaches for the schema owner, and importing the engine at
        # module scope would make every backup command pay for Alembic.
        from blitzecdn.core.database_engine import Database

        database = Database(destination)
        database.close()


class _DirectoryComponent:
    """A component that is one directory on disk, copied whole."""

    component: BackupComponent
    requires_offline = False

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def _root(self) -> Path:
        raise NotImplementedError

    def present(self) -> bool:
        root = self._root
        return root.is_dir() and any(root.iterdir())

    def export(self, staging: Path) -> None:
        links: dict[str, str] = {}
        _copy_tree(self._root, staging, links=links)
        if links:
            atomic_write_bytes(
                staging / LINKS_MEMBER,
                json.dumps(links, indent=2, sort_keys=True).encode("utf-8") + b"\n",
            )

    def validate(self, staging: Path) -> None:
        if not staging.is_dir():
            raise ConfigurationError(
                f"backup declares {self.component.value} but the archive has no "
                f"{self.component.value}/ directory"
            )
        links = _read_links(staging)
        files = [
            path
            for path in staging.rglob("*")
            if path.is_file() and path.name != LINKS_MEMBER
        ]
        if not files:
            detail = "only link metadata" if links else "no files"
            raise ConfigurationError(
                f"backup declares {self.component.value} but it contains {detail}"
            )

    def restore(self, staging: Path) -> None:
        """Build beside the destination and swap it in with rollback."""
        links = _read_links(staging)
        destination = self._root
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        incoming = destination.with_name(f".{destination.name}.{uuid4().hex}")
        previous = destination.with_name(f".{destination.name}.previous.{uuid4().hex}")
        try:
            _copy_tree(staging, incoming, links={})
            (incoming / LINKS_MEMBER).unlink(missing_ok=True)
            _restore_links(incoming, links)
            if destination.exists():
                destination.rename(previous)
            try:
                incoming.rename(destination)
            except OSError:
                if previous.exists():
                    previous.rename(destination)
                raise
            rmtree(previous, ignore_errors=True)
        finally:
            rmtree(incoming, ignore_errors=True)
            # Present only if replacing the old tree succeeded and its cleanup
            # was interrupted. It no longer participates in live state.
            rmtree(previous, ignore_errors=True)


class TlsComponent(_DirectoryComponent):
    """Installed certificate chains, their private keys, and their metadata.

    The keys come back `0600` because `atomic_write_bytes` creates every file
    at that mode before writing it — the same write path that installed them in
    the first place, rather than a chmod after the fact that would leave a
    window in which a private key was readable.
    """

    component = BackupComponent.TLS

    @property
    def _root(self) -> Path:
        return self._settings.certificate_dir


class AcmeComponent(_DirectoryComponent):
    """The local ACME account, its renewal configuration, and its archive.

    Certbot's *working* and *log* directories are deliberately excluded: both
    are rebuilt on the next run and neither is state anything depends on.
    """

    component = BackupComponent.ACME

    @property
    def _root(self) -> Path:
        return self._settings.state_dir / "letsencrypt/config"


class ConfigComponent:
    """Operator configuration and the controller's own identity.

    Four files in three places, so this is a named set rather than a directory:
    the non-secret settings file, the secrets that are deliberately not valid
    TOML keys, and the SSH key pair every edge has already authorised. The key
    is here because regenerating it means reaching every edge to install the
    new one — using the key you no longer have.
    """

    component = BackupComponent.CONFIG
    #: The services read `.env` once, at start.
    requires_offline = True

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def _files(self) -> dict[str, Path]:
        project = self._settings.project_dir
        state = self._settings.state_dir
        return {
            "blitzecdn.toml": project / "blitzecdn.toml",
            "env": self._settings.environment_path,
            "controller_key": state / "id_ed25519",
            "controller_key.pub": state / "id_ed25519.pub",
        }

    def present(self) -> bool:
        config = self._files["blitzecdn.toml"]
        if config.is_file() and any(
            key in PORTABLE_CONFIG_KEYS for key in _toml_document(config.read_bytes())
        ):
            return True
        environment = self._files["env"]
        if environment.is_file() and any(
            key in PORTABLE_ENVIRONMENT_KEYS
            for key in _environment_assignments(environment.read_bytes())
        ):
            return True
        return any(
            self._files[name].is_file()
            for name in ("controller_key", "controller_key.pub")
        )

    def export(self, staging: Path) -> None:
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, path in self._files.items():
            if path.is_file() and not path.is_symlink():
                payload = path.read_bytes()
                if name == "blitzecdn.toml":
                    values = _toml_document(payload)
                    payload = _render_toml(
                        {
                            key: value
                            for key, value in values.items()
                            if key in PORTABLE_CONFIG_KEYS
                        }
                    )
                elif name == "env":
                    assignments = _environment_assignments(payload)
                    payload = _render_environment(
                        {
                            key: value
                            for key, value in assignments.items()
                            if key in PORTABLE_ENVIRONMENT_KEYS
                        }
                    )
                atomic_write_bytes(staging / name, payload)

    def validate(self, staging: Path) -> None:
        if not staging.is_dir():
            raise ConfigurationError(
                f"backup declares {self.component.value} but the archive has no "
                f"{self.component.value}/ directory"
            )
        known = set(self._files)
        unknown = sorted(
            child.name for child in staging.iterdir() if child.name not in known
        )
        if unknown:
            raise ConfigurationError(
                f"{self.component.value}/ holds files this installation does not "
                f"recognise: {', '.join(unknown)}"
            )
        config = staging / "blitzecdn.toml"
        meaningful = False
        if config.is_file():
            keys = set(_toml_document(config.read_bytes()))
            unsafe = sorted(keys & MACHINE_SPECIFIC_CONFIG_KEYS)
            unknown = sorted(keys - PORTABLE_CONFIG_KEYS)
            if unsafe or unknown:
                names = unsafe or unknown
                raise ConfigurationError(
                    "config/blitzecdn.toml contains non-portable keys: "
                    + ", ".join(names)
                )
            meaningful = meaningful or bool(keys)
        environment = staging / "env"
        if environment.is_file():
            keys = set(_environment_assignments(environment.read_bytes()))
            unknown_env = sorted(keys - PORTABLE_ENVIRONMENT_KEYS)
            if unknown_env:
                raise ConfigurationError(
                    "config/env contains non-portable keys: " + ", ".join(unknown_env)
                )
            meaningful = meaningful or bool(keys)
        private = staging / "controller_key"
        public = staging / "controller_key.pub"
        if private.is_file() != public.is_file():
            raise ConfigurationError("config/ must contain both controller SSH keys")
        meaningful = meaningful or (private.is_file() and public.is_file())
        if not meaningful:
            raise ConfigurationError(f"{self.component.value}/ is empty")

    def restore(self, staging: Path) -> None:
        for name, path in self._files.items():
            source = staging / name
            if source.is_file():
                payload = source.read_bytes()
                if name == "blitzecdn.toml":
                    portable = _toml_document(payload)
                    current = (
                        _toml_document(path.read_bytes()) if path.is_file() else {}
                    )
                    machine = {
                        key: value
                        for key, value in current.items()
                        if key in MACHINE_SPECIFIC_CONFIG_KEYS
                    }
                    payload = _render_toml({**portable, **machine})
                elif name == "env":
                    portable_env = _environment_assignments(payload)
                    current_env = (
                        _environment_assignments(path.read_bytes())
                        if path.is_file()
                        else {}
                    )
                    payload = _render_environment({**current_env, **portable_env})
                # `.pub` is not secret, but writing it 0600 costs nothing and
                # keeps one write path for the pair.
                atomic_write_bytes(path, payload)


__all__ = [
    "DATABASE_MEMBER",
    "LINKS_MEMBER",
    "AcmeComponent",
    "ConfigComponent",
    "DatabaseComponent",
    "TlsComponent",
]
