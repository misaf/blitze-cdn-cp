"""Keep the exact previous files until a restore is fully published."""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from blitzecdn.core.exceptions import ConfigurationError, ExecutionError
from blitzecdn.core.runtime.filesystem import atomic_write_bytes


class RestoreJournal:
    """A private, persistent checkpoint, also marking an unfinished restore.

    Unlike an exported backup, this includes machine-specific configuration,
    file modes, symlinks and absent paths. It is never removed after a failed
    rollback. A managed restore keeps it until the host publishes configuration.
    """

    def __init__(self, root: Path, *, defer_commit: bool = False) -> None:
        self.root = root
        self.defer_commit = defer_commit

    @contextmanager
    def protect(self, paths: Sequence[Path]) -> Iterator[None]:
        self._capture(paths)
        try:
            yield
        except BaseException:
            self.rollback()
            self.commit()
            raise
        else:
            if not self.defer_commit:
                self.commit()

    def _capture(self, paths: Sequence[Path]) -> None:
        if any(
            not path.is_symlink() and self.root.resolve().is_relative_to(path.resolve())
            for path in paths
        ):
            raise ConfigurationError("restore checkpoint cannot be inside a saved tree")
        self.root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.root.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ConfigurationError(
                f"an unfinished restore is retained at {self.root}; "
                "run 'blitzecdn backup recover' before restoring again"
            ) from exc
        try:
            originals = []
            for index, path in enumerate(dict.fromkeys(paths)):
                present = path.exists() or path.is_symlink()
                if present:
                    self._copy(path, self.root / str(index))
                originals.append({"path": str(path.absolute()), "present": present})
            atomic_write_bytes(
                self.root / "manifest.json", json.dumps(originals).encode("utf-8")
            )
        except BaseException:
            # No live writes have started when capturing fails.
            shutil.rmtree(self.root)
            raise

    def rollback(self) -> None:
        """Recover every path, retaining the checkpoint even after success."""
        try:
            originals = json.loads((self.root / "manifest.json").read_text())
        except (OSError, ValueError) as exc:
            raise ExecutionError(
                f"cannot read restore checkpoint at {self.root}; keep services stopped"
            ) from exc
        if not isinstance(originals, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and Path(item["path"]).is_absolute()
            and isinstance(item.get("present"), bool)
            for item in originals
        ):
            raise ExecutionError(f"invalid restore checkpoint at {self.root}")
        failures = []
        for index in reversed(range(len(originals))):
            item = originals[index]
            path = Path(item["path"])
            try:
                saved = self.root / str(index)
                if item["present"] and not (saved.exists() or saved.is_symlink()):
                    raise OSError(f"checkpoint is missing {saved}")
                self._remove(path)
                if item["present"]:
                    self._copy(saved, path)
            except OSError as exc:
                failures.append(f"{path}: {exc}")
        if failures:
            raise ExecutionError(
                f"restore recovery failed; previous state remains at {self.root}; "
                "keep services stopped: " + "; ".join(failures)
            )

    def commit(self) -> None:
        """Discard previous state only after application and host publication."""
        if self.root.exists():
            committed = self.root.with_name(f".restore-committed-{uuid4().hex}")
            self.root.rename(committed)
            shutil.rmtree(committed, ignore_errors=True)

    @staticmethod
    def _remove(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    @staticmethod
    def _copy(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
