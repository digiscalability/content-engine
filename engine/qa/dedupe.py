"""FR-QA-3 self-dedupe: a script too close to one of the identity's own recent posts is a
hard fail. ADR 0018.

A post counts once it could go out: QA held or passed it, or it went further (drafted,
uploaded, scheduled, published). Failed and rejected posts never went out, so a later post
may reuse their idea. The comparison is against the render each counted post carries,
newest first, one per brief, up to `lookback` briefs.
"""

from __future__ import annotations

import sqlite3

import numpy as np

from engine.library.embeddings import cosine, from_blob
from engine.qa import CheckResult, verdict

COUNTED_STATES = ("qa_held", "qa_passed", "drafted", "uploaded_private", "scheduled", "published")
Earlier = tuple[int, str, np.ndarray]  # brief id, topic, script embedding


def recent_scripts(
    conn: sqlite3.Connection, identity_id: str, brief_id: int, lookback: int
) -> list[Earlier]:
    """The identity's last `lookback` counted posts, newest first, other than this brief."""
    marks = ",".join("?" * len(COUNTED_STATES))
    rows = conn.execute(
        f"""SELECT b.id AS brief_id, b.topic, r.script_embedding
            FROM renders r
            JOIN edit_plans e ON e.id = r.edit_plan_id
            JOIN briefs b ON b.id = e.brief_id
            WHERE b.identity_id = ? AND b.id != ? AND r.script_embedding IS NOT NULL
              AND EXISTS (SELECT 1 FROM posts p WHERE p.render_id = r.id
                          AND p.state IN ({marks}))
            ORDER BY r.id DESC""",
        (identity_id, brief_id, *COUNTED_STATES),
    )
    seen: dict[int, Earlier] = {}
    for row in rows:
        if row["brief_id"] not in seen and len(seen) < lookback:
            embedding = from_blob(row["script_embedding"])
            seen[row["brief_id"]] = (row["brief_id"], row["topic"], embedding)
    return list(seen.values())


def self_dedupe_check(vector: np.ndarray, earlier: list[Earlier], limit: float) -> CheckResult:
    fails = []
    for brief_id, topic, other in earlier:
        if other.shape != vector.shape:
            continue  # made by another embedding model: nothing to compare
        score = cosine(vector, other)
        if score > limit:
            fails.append(
                f"the script is {score:.2f} similar to brief {brief_id} ('{topic}'), above "
                f"the {limit:.2f} limit (FR-QA-3)"
            )
    return verdict("self_dedupe", fails)
