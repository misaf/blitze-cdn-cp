"""The read-only schema probe a failed update compares across its migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from blitzecdn.host import schema_revision


def _probe(monkeypatch: pytest.MonkeyPatch, database: Path) -> None:
    monkeypatch.setattr(
        schema_revision.Settings,
        "from_environment",
        classmethod(lambda cls: SimpleNamespace(database_path=database)),
    )


def test_prints_the_stamped_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    database = tmp_path / "control-plane.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
    connection.execute("INSERT INTO alembic_version VALUES ('0007_example')")
    connection.commit()
    connection.close()
    _probe(monkeypatch, database)

    schema_revision.main()

    assert capsys.readouterr().out == "0007_example\n"


def test_prints_an_empty_line_before_the_first_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    database = tmp_path / "control-plane.db"
    _probe(monkeypatch, database)

    schema_revision.main()

    assert capsys.readouterr().out == "\n"
    assert not database.exists(), "the probe must never create the database"
