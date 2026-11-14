"""The manual kill switch (GR-7, §17): `ce kill`, `ce warn` and `ce resume`. ADR 0019.

A kill sets the identity's kill flag with a reason and drops it to L0 in the database; the
agent tools then refuse it any new work (`require_alive`) and H1 refuses to schedule. A YAML
sync never lifts the drop. `ce warn` records a platform warning that arrived (Postiz cannot
see them) and kills. `ce resume` is the only way back: it clears the flag and restores the
level the YAML gives. Every step is a row in `identity_events`.

The engine does not touch Postiz here. `in_flight()` lists the identity's posts that could
still go out (drafted, uploaded privately, scheduled) so Abbas can pull them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from engine.config import Config

STILL_OUT = ("drafted", "uploaded_private", "scheduled")


class KillSwitchError(ValueError):
    """The command cannot apply: an unknown identity, no reason, or nothing to resume."""


@dataclass(frozen=True)
class InFlight:
    post_id: int
    platform: str
    state: str
    postiz_id: str | None
    scheduled_at: str | None


def _require(conn: sqlite3.Connection, identity_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM identities WHERE id = ?", (identity_id,)).fetchone()
    if row is None:
        raise KillSwitchError(f"no identity '{identity_id}' (see config/identities/)")
    return row


def _event(conn: sqlite3.Connection, identity_id: str, kind: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO identity_events (identity_id, kind, detail) VALUES (?, ?, ?)",
        (identity_id, kind, detail),
    )


def events(conn: sqlite3.Connection, identity_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT kind, detail, created_at FROM identity_events WHERE identity_id = ? ORDER BY id",
        (identity_id,),
    ).fetchall()


def in_flight(conn: sqlite3.Connection, identity_id: str) -> list[InFlight]:
    marks = ",".join("?" * len(STILL_OUT))
    rows = conn.execute(
        f"""SELECT p.id, c.platform, p.state, p.postiz_id, p.scheduled_at
            FROM posts p JOIN channels c ON c.id = p.channel_id
            WHERE c.identity_id = ? AND p.state IN ({marks}) ORDER BY p.id""",
        (identity_id, *STILL_OUT),
    )
    return [InFlight(*row) for row in rows]


def kill(conn: sqlite3.Connection, identity_id: str, reason: str) -> list[InFlight]:
    """Set the kill flag and drop to L0; returns the posts that could still go out."""
    _require(conn, identity_id)
    if not reason.strip():
        raise KillSwitchError("a kill needs a reason")
    conn.execute(
        "UPDATE identities SET kill_flag = 1, kill_reason = ?, autonomy_level = 0 WHERE id = ?",
        (reason.strip(), identity_id),
    )
    _event(conn, identity_id, "kill", reason.strip())
    return in_flight(conn, identity_id)


def warn(conn: sqlite3.Connection, identity_id: str, text: str) -> list[InFlight]:
    """Record a platform warning, strike or demonetisation notice, then kill (GR-7)."""
    _require(conn, identity_id)
    if not text.strip():
        raise KillSwitchError("a warning needs the text of what arrived")
    _event(conn, identity_id, "warn", text.strip())
    return kill(conn, identity_id, f"platform warning: {text.strip()}")


def resume(conn: sqlite3.Connection, config: Config, identity_id: str) -> int:
    """Clear the kill flag and restore the YAML's autonomy level, which it returns."""
    row = _require(conn, identity_id)
    if not row["kill_flag"]:
        raise KillSwitchError(f"{identity_id} is not paused")
    identity = config.identities.get(identity_id)
    if identity is None:
        raise KillSwitchError(f"{identity_id} is no longer in config/identities/")
    conn.execute(
        "UPDATE identities SET kill_flag = 0, kill_reason = NULL, autonomy_level = ? WHERE id = ?",
        (identity.autonomy_level, identity_id),
    )
    _event(conn, identity_id, "resume", f"resumed at L{identity.autonomy_level}")
    return identity.autonomy_level
