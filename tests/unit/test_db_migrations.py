"""Versioned migrations (SRD §11): numbered SQL files, PRAGMA user_version, atomic per file."""

import sqlite3
from pathlib import Path

import pytest

from engine.db import MigrationError, connect, migrate, open_db, schema_version


def write_migrations(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, sql in files.items():
        (directory / name).write_text(sql, encoding="utf-8")
    return directory


def tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def test_migrate_applies_in_order_and_sets_version(tmp_path):
    mig = write_migrations(
        tmp_path / "m",
        {
            "0001_first.sql": "CREATE TABLE a (id INTEGER PRIMARY KEY);",
            "0002_second.sql": "CREATE TABLE b (a_id INTEGER REFERENCES a(id));",
        },
    )
    conn = connect(tmp_path / "e.db")
    assert migrate(conn, mig) == [1, 2]
    assert schema_version(conn) == 2
    assert {"a", "b"} <= tables(conn)


def test_migrate_is_idempotent(tmp_path):
    mig = write_migrations(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a (id INTEGER);"})
    conn = connect(tmp_path / "e.db")
    migrate(conn, mig)
    assert migrate(conn, mig) == []
    assert schema_version(conn) == 1


def test_failed_migration_rolls_back(tmp_path):
    mig = write_migrations(
        tmp_path / "m",
        {
            "0001_first.sql": "CREATE TABLE a (id INTEGER);",
            "0002_broken.sql": "CREATE TABLE b (id INTEGER);\nCREATE TABLE oops (;",
        },
    )
    conn = connect(tmp_path / "e.db")
    with pytest.raises(MigrationError, match="0002_broken.sql"):
        migrate(conn, mig)
    assert schema_version(conn) == 1
    assert "b" not in tables(conn)
    assert not conn.in_transaction


def test_numbering_gap_rejected(tmp_path):
    mig = write_migrations(
        tmp_path / "m",
        {"0001_first.sql": "SELECT 1;", "0003_third.sql": "SELECT 1;"},
    )
    with pytest.raises(MigrationError, match="gap"):
        migrate(connect(tmp_path / "e.db"), mig)


def test_newer_db_than_code_refused(tmp_path):
    mig = write_migrations(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a (id INTEGER);"})
    conn = connect(tmp_path / "e.db")
    conn.execute("PRAGMA user_version = 5")
    with pytest.raises(MigrationError, match="newer"):
        migrate(conn, mig)
    assert "a" not in tables(conn)


def test_connect_enables_foreign_keys_and_wal(tmp_path):
    conn = connect(tmp_path / "e.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.row_factory is sqlite3.Row


def test_open_db_creates_parent_dir_and_migrates(tmp_path):
    mig = write_migrations(tmp_path / "m", {"0001_first.sql": "CREATE TABLE a (id INTEGER);"})
    conn = open_db(tmp_path / "data" / "engine.db", mig)
    assert (tmp_path / "data" / "engine.db").is_file()
    assert schema_version(conn) == 1
