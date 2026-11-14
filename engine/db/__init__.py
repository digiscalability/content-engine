"""SQLite access: connections and versioned migrations (SRD §11).

Migrations are plain SQL files `engine/db/migrations/NNNN_name.sql`, numbered from 0001
with no gaps. The applied version lives in `PRAGMA user_version`. Each file runs in one
transaction together with its version bump, so a failing file leaves the database at
the previous version. Migration files never contain BEGIN or COMMIT themselves.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import sqlite_vec

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_FILE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(Exception):
    """The migration files or the database's version are not in a state we can apply."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILE.match(path.name)
        if not match:
            raise MigrationError(f"{path.name}: migration files are named NNNN_name.sql")
        found.append(Migration(int(match.group(1)), path.name, path.read_text(encoding="utf-8")))
    for expected, migration in enumerate(found, start=1):
        if migration.version != expected:
            raise MigrationError(
                f"{migration.name}: numbering gap or duplicate, expected {expected:04d}"
            )
    return found


def connect(path: Path | str) -> sqlite3.Connection:
    """A connection with foreign keys on, WAL journaling, explicit transactions and the
    sqlite-vec extension (the library's vector tables, migration 0005)."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply every pending migration in order; return the versions applied."""
    migrations = load_migrations(directory)
    current = schema_version(conn)
    latest = migrations[-1].version if migrations else 0
    if current > latest:
        raise MigrationError(
            f"database is at version {current}, newer than this engine's {latest}; "
            "update the engine instead of opening it with an older one"
        )
    applied: list[int] = []
    for migration in migrations[current:]:
        script = f"BEGIN;\n{migration.sql}\n;\nPRAGMA user_version = {migration.version};\nCOMMIT;"
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"{migration.name}: {exc}") from exc
        applied.append(migration.version)
    return applied


def open_db(path: Path, directory: Path = MIGRATIONS_DIR) -> sqlite3.Connection:
    """Connect to the engine database, creating it and applying pending migrations."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    migrate(conn, directory)
    return conn
