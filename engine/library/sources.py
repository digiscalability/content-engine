"""The sources config mirrored into the `sources` table (FR-SRC-1..3). Only Abbas edits it;
this copies it so assets can reference their source and carry its tier. A tier change on a
source that already has assets is refused by the schema and reported here by name."""

from __future__ import annotations

import sqlite3

from engine.config import Config

FIELDS = ("kind", "path", "volume_serial", "volume_label", "url", "rights_type",
          "rights_proof", "licence_url", "attribution", "expires_at")  # fmt: skip


def sync_sources(conn: sqlite3.Connection, config: Config) -> None:
    for source in config.sources:
        values = [getattr(source, f) for f in FIELDS]
        values[FIELDS.index("expires_at")] = (
            source.expires_at.isoformat() if source.expires_at else None
        )
        updates = ", ".join(f"{f} = excluded.{f}" for f in FIELDS)
        try:
            conn.execute(
                f"INSERT INTO sources (id, {', '.join(FIELDS)}) "
                f"VALUES (?, {', '.join('?' * len(FIELDS))}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                (source.id, *values),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"sources config: source '{source.id}': {exc}") from exc
