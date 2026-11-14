"""Posts seeded straight into a test DB, for the reconciliation, metrics and digest tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from engine.agents.state import sync_identities
from engine.config import load_config
from engine.db import open_db

INTEGRATIONS = {"youtube": "yt-1", "tiktok": "tt-1"}  # FakePostiz's channels


def seeded_db(path: Path, repo_root: Path) -> sqlite3.Connection:
    """The real config's identities, with selfhelp on YouTube and TikTok in the fake Postiz."""
    conn = open_db(path)
    sync_identities(conn, load_config(repo_root / "config"))
    conn.execute("UPDATE channels SET postiz_integration_id = 'yt-1' WHERE platform = 'youtube'")
    conn.execute(
        "INSERT INTO channels (identity_id, platform, postiz_integration_id, ai_disclosure) "
        "VALUES ('selfhelp', 'tiktok', 'tt-1', 'required')"
    )
    return conn


def seed_post(
    conn: sqlite3.Connection,
    state: str,
    platform: str = "youtube",
    ai_disclosure: int = 1,
    **fields,
) -> int:
    brief = conn.execute(
        "INSERT INTO briefs (identity_id, format, topic, angle, hook, cta, difference_note) "
        "VALUES ('selfhelp', 'C', 'starting small', 'craft', 'hook', 'follow', 'new')"
    ).lastrowid
    plan = conn.execute(
        "INSERT INTO edit_plans (brief_id, version, plan) VALUES (?, 1, '{}')", (brief,)
    ).lastrowid
    render = conn.execute(
        "INSERT INTO renders (edit_plan_id, path) VALUES (?, ?)", (plan, f"post-{brief}.mp4")
    ).lastrowid
    channel = conn.execute("SELECT id FROM channels WHERE platform = ?", (platform,)).fetchone()[0]
    cols = {"brief_id": brief, "channel_id": channel, "render_id": render, "state": state,
            "ai_disclosure": ai_disclosure, **fields}  # fmt: skip
    return conn.execute(
        f"INSERT INTO posts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        list(cols.values()),
    ).lastrowid


def row(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
