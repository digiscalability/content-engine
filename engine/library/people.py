"""The people rule's review list and Abbas's marks (SRD FR-UND-5, §8.3, C13). ADR 0022.

An asset whose people check saw a face keeps `people_release = unknown`, which keeps it out
of B-roll search. `ce status --people` lists them; Abbas marks each `yes` (consent held) or
`exclude`. Every mark is a `people_marks` row; the asset carries the latest one.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

DECISIONS = ("yes", "exclude")


@dataclass(frozen=True)
class PeopleItem:
    asset_id: int
    source_id: str
    path: str
    duration: float | None
    faces: int
    first_face_s: float | None
    keyframe: str | None  # the keyframe of the scene where the first face was seen


def review_list(conn: sqlite3.Connection) -> list[PeopleItem]:
    """Checked assets that showed a face and have no mark yet, oldest first."""
    rows = conn.execute(
        """SELECT a.id, a.source_id, a.path, a.duration, i.faces, i.face_first_s
           FROM assets a JOIN asset_index i ON i.asset_id = a.id
           WHERE i.people_at IS NOT NULL AND a.people_release = 'unknown'
           ORDER BY a.id"""
    ).fetchall()
    return [PeopleItem(r["id"], r["source_id"], r["path"], r["duration"], r["faces"] or 0,
                       r["face_first_s"], _keyframe(conn, r["id"], r["face_first_s"]))
            for r in rows]  # fmt: skip


def _keyframe(conn: sqlite3.Connection, asset_id: int, at: float | None) -> str | None:
    row = conn.execute(
        "SELECT keyframe_path FROM scenes WHERE asset_id = ? AND keyframe_path IS NOT NULL "
        "ORDER BY (? NOT BETWEEN start_s AND end_s), start_s LIMIT 1",
        (asset_id, at or 0.0),
    ).fetchone()
    return row[0] if row else None


def mark(conn: sqlite3.Connection, asset_id: int, decision: str, note: str = "") -> None:
    """Record Abbas's decision for one asset and make it the asset's state."""
    if decision not in DECISIONS:
        raise ValueError(f"a people mark is yes or exclude, not {decision!r}")
    if conn.execute("SELECT 1 FROM assets WHERE id = ?", (asset_id,)).fetchone() is None:
        raise ValueError(f"no asset {asset_id}")
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO people_marks (asset_id, decision, note) VALUES (?, ?, ?)",
            (asset_id, decision, note),
        )
        conn.execute("UPDATE assets SET people_release = ? WHERE id = ?", (decision, asset_id))
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
