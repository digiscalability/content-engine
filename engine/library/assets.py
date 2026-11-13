"""Assets the engine makes itself (ADR 0012).

Voice WAVs and avatar videos become rows of the built-in `engine` source (migration 0002),
tier `ai_generated`, people `none` (the only face is Abbas's avatar, §8.3), so an edit plan
names them by asset id like any footage and the rights check covers them too.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Literal

ENGINE_SOURCE = "engine"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def register_generated(
    conn: sqlite3.Connection,
    path: Path,
    media_kind: Literal["video", "audio"],
    duration: float,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
) -> int:
    """The asset id for a generated file; the same bytes always map to the same asset."""
    sha = file_sha256(path)
    conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, duration, width, height, fps, "
        "rights_type, people_release) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ai_generated', 'none') "
        "ON CONFLICT(sha256) DO NOTHING",
        (ENGINE_SOURCE, sha, str(path.resolve()), media_kind, duration, width, height, fps),
    )
    return int(conn.execute("SELECT id FROM assets WHERE sha256 = ?", (sha,)).fetchone()[0])
