"""The DB mirror of identity config (SRD §11 `identities`, `channels`).

`autonomy_level` always comes from the YAML, which only Abbas edits (§17); the kill
flag lives only in the DB (`ce kill`, `ce warn`), so a sync never clears it.
"""

from __future__ import annotations

import sqlite3

from engine.config import Config


def start_run(conn: sqlite3.Connection, run_id: str, command: str) -> None:
    """The `agent_runs` row every tool call of the run points at (NFR-3)."""
    conn.execute(
        "INSERT OR IGNORE INTO agent_runs (run_id, command) VALUES (?, ?)", (run_id, command)
    )


def sync_identities(conn: sqlite3.Connection, config: Config) -> None:
    for identity in config.identities.values():
        conn.execute(
            "INSERT INTO identities (id, display_name, autonomy_level) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name, "
            "autonomy_level = excluded.autonomy_level",
            (identity.id, identity.display_name, identity.autonomy_level),
        )
        for channel in identity.channels:
            conn.execute(
                "INSERT INTO channels "
                "(identity_id, platform, postiz_integration_id, ai_disclosure) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(identity_id, platform) DO UPDATE SET "
                "postiz_integration_id = excluded.postiz_integration_id, "
                "ai_disclosure = excluded.ai_disclosure",
                (
                    identity.id,
                    channel.platform,
                    channel.postiz_integration_id,
                    channel.ai_disclosure,
                ),
            )


# Post rows (§10) ----------------------------------------------------------------------------

# Forward order of the §10 states; states sharing a rank are alternatives at that step.
POST_RANK = {
    "planned": 0,
    "scripted": 1,
    "voiced": 2,
    "avatar_waiting": 3,
    "avatar_ready": 4,
    "rendered": 5,
    "qa_failed": 6,
    "qa_held": 6,
    "qa_passed": 6,
    "drafted": 7,
    "uploaded_private": 7,
    "scheduled": 8,
    "published": 9,
}
ENDED = ("published", "rejected", "failed")


def plan_posts(conn: sqlite3.Connection, brief_id: int, identity_id: str, fmt: str) -> None:
    """One `planned` post per channel of the identity; format C posts carry the avatar, so
    they are always disclosed as AI (FR-QA-5)."""
    for channel in conn.execute(
        "SELECT id, ai_disclosure FROM channels WHERE identity_id = ?", (identity_id,)
    ):
        disclose = int(fmt == "C" or channel["ai_disclosure"] == "required")
        conn.execute(
            "INSERT OR IGNORE INTO posts (brief_id, channel_id, ai_disclosure) VALUES (?, ?, ?)",
            (brief_id, channel["id"], disclose),
        )


def advance_posts(
    conn: sqlite3.Connection, brief_id: int, state: str, of_render: int | None = None, **fields
) -> int:
    """Move a brief's posts to `state` (and set `fields`), strictly forward: never back,
    never sideways between alternatives (a held or failed post never becomes passed), never
    out of an ended state. `of_render` limits the move to the posts on that render, so a
    verdict on one render cannot move posts that are on another. Returns how many moved."""
    moved = 0
    assignments = "".join(f", {name} = ?" for name in fields)
    for row in conn.execute(
        "SELECT id, state, render_id FROM posts WHERE brief_id = ?", (brief_id,)
    ):
        if of_render is not None and row["render_id"] != of_render:
            continue
        current = row["state"]
        if current in ENDED or not (current == state or POST_RANK[state] > POST_RANK[current]):
            continue
        conn.execute(
            f"UPDATE posts SET state = ?{assignments}, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (state, *fields.values(), row["id"]),
        )
        moved += 1
    return moved


def fail_posts(conn: sqlite3.Connection, brief_id: int, reason: str) -> None:
    """End a brief's posts that have not been rendered yet as `failed`, keeping the reason
    for the digest and retro. A late failure (an old avatar request expiring) never touches
    a post that is already rendered, drafted or live."""
    early = [state for state, rank in POST_RANK.items() if rank < POST_RANK["rendered"]]
    placeholders = ", ".join("?" * len(early))
    conn.execute(
        f"UPDATE posts SET state = 'failed', reject_reason = ?, "
        f"updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
        f"WHERE brief_id = ? AND state IN ({placeholders})",
        (reason, brief_id, *early),
    )
