"""`ce reconcile` (SRD §10 step 3, FR-PUB-3, NFR-1; ADR 0021): every post the engine
drafted follows what Abbas did with it in Postiz and YouTube Studio.

- One `posts:list` call covers every open post: scheduled → `scheduled`, live →
  `published` (or `uploaded_private` on YouTube's private route), error → `failed`, gone →
  `rejected`.
- Gone means gone only on a full view: if Postiz lists nothing at all for a channel (as
  when the channel was disconnected), nothing on it is rejected and the run warns. A post
  rejected that way comes back if it reappears within 30 days, unless Abbas gave a reason
  with `ce reject`, which is final.
- A published post is never moved back; Postiz may only fill in its platform id.
- A private-route YouTube post becomes `published` the first time YouTube says the video is
  public; that moment is its `published_at`.

All Postiz-driven changes are written in one transaction after the list is read, so a
failing call changes nothing, and a rerun with the same answers changes nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from engine.publish.postiz import MISSING, Publisher, RemotePost, engine_state
from engine.publish.youtube import Visibility, VisibilityError

FOLLOWED = ("drafted", "scheduled", "failed")  # rejected when Postiz no longer has them
LIVE = ("published", "uploaded_private")  # never moved back
REJECTABLE = ("drafted", "scheduled", "uploaded_private", "rejected")  # by `ce reject`
RESTORE_WITHIN = timedelta(days=30)
WINDOW_BEFORE = timedelta(days=30)
WINDOW_AFTER = timedelta(days=365)
REASON_MAX = 300


class ReconcileError(RuntimeError):
    """A post cannot take the change asked for."""


@dataclass(frozen=True)
class Change:
    post_id: int
    before: str
    after: str


@dataclass
class ReconcileReport:
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OpenPost:
    post_id: int
    state: str
    platform: str
    integration_id: str | None
    postiz_id: str
    platform_id: str | None
    private_route: bool  # YouTube with AI disclosure goes up private (C15)
    since: datetime  # the earliest of its creation and its slot, for the list window
    slot: datetime | None


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def open_posts(conn: sqlite3.Connection, now: datetime) -> list[OpenPost]:
    """The posts a reconcile run checks, in id order."""
    rows = conn.execute(
        "SELECT p.id, p.state, p.postiz_id, p.platform_id, p.ai_disclosure, p.scheduled_at, "
        "p.created_at, c.platform, c.postiz_integration_id FROM posts p "
        "JOIN channels c ON c.id = p.channel_id WHERE p.postiz_id IS NOT NULL AND ("
        f"p.state IN {FOLLOWED} OR p.state = 'uploaded_private' "
        "OR (p.state = 'published' AND (p.platform_id IS NULL OR p.platform_id = ?)) "
        "OR (p.state = 'rejected' AND p.reject_reason IS NULL "
        "AND COALESCE(p.updated_at, p.created_at) >= ?)) ORDER BY p.id",
        (MISSING, stamp(now - RESTORE_WITHIN)),
    ).fetchall()
    posts = []
    for r in rows:
        slot = parse(r["scheduled_at"])
        created = parse(r["created_at"]) or now
        posts.append(
            OpenPost(
                post_id=r["id"],
                state=r["state"],
                platform=r["platform"],
                integration_id=r["postiz_integration_id"],
                postiz_id=r["postiz_id"],
                platform_id=r["platform_id"],
                private_route=r["platform"] == "youtube" and bool(r["ai_disclosure"]),
                since=min(created, slot) if slot else created,
                slot=slot,
            )
        )
    return posts


def _needs_postiz(post: OpenPost) -> bool:
    return not (post.state == "uploaded_private" and post.platform_id)


def _from_remote(post: OpenPost, remote: RemotePost, now: datetime) -> dict:
    """The columns that change for what Postiz reports (empty when nothing does)."""
    release = remote.release_id
    if post.state in LIVE:
        if release and release != MISSING and post.platform_id in (None, MISSING):
            return {"platform_id": release}
        if release == MISSING and post.platform_id is None:
            return {"platform_id": MISSING}
        return {}
    after = engine_state(remote, post.private_route)
    update: dict = {"state": after}
    if remote.publish_date:
        update["scheduled_at"] = stamp(remote.publish_date)
    if after in LIVE and release:
        update["platform_id"] = release
    if after == "published":
        update["published_at"] = stamp(remote.publish_date or now)
    if after == "failed":
        update["reject_reason"] = f"Postiz: {remote.error or 'error'}"[:REASON_MAX]
    elif post.state == "failed":
        update["reject_reason"] = None
    return update


def _write(conn: sqlite3.Connection, post_id: int, update: dict, now: datetime) -> bool:
    current = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    changed = {k: v for k, v in update.items() if current[k] != v}
    if not changed:
        return False
    changed["updated_at"] = stamp(now)
    sets = ", ".join(f"{column} = ?" for column in changed)
    conn.execute(f"UPDATE posts SET {sets} WHERE id = ?", [*changed.values(), post_id])
    return True


def reconcile(
    conn: sqlite3.Connection, publisher: Publisher, visibility: Visibility, now: datetime
) -> ReconcileReport:
    report = ReconcileReport()
    posts = [p for p in open_posts(conn, now) if _needs_postiz(p)]
    if posts:
        start = min(p.since for p in posts) - WINDOW_BEFORE
        end = max([now, *(p.slot for p in posts if p.slot)]) + WINDOW_AFTER
        remote = {r.post_id: r for r in publisher.list_posts(start, end)}
        listed = {r.integration_id for r in remote.values()}
        updates: list[tuple[OpenPost, dict]] = []
        blind: set[str] = set()
        for post in posts:
            found = remote.get(post.postiz_id)
            if found is not None:
                updates.append((post, _from_remote(post, found, now)))
            elif post.state in FOLLOWED:
                if post.integration_id in listed:
                    updates.append((post, {"state": "rejected", "reject_reason": None}))
                else:
                    blind.add(post.integration_id or f"{post.platform} (no integration id)")
        conn.execute("BEGIN")
        try:
            for post, update in updates:
                after = update.get("state", post.state)
                if _write(conn, post.post_id, update, now) and after != post.state:
                    report.changes.append(Change(post.post_id, post.state, after))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        for integration in sorted(blind):
            report.warnings.append(
                f"Postiz listed no posts for channel {integration}, so none of its posts were "
                "marked rejected; check the channel is still connected in Postiz"
            )
    _check_youtube(conn, visibility, now, report)
    return report


def _check_youtube(
    conn: sqlite3.Connection, visibility: Visibility, now: datetime, report: ReconcileReport
) -> None:
    rows = conn.execute(
        "SELECT p.id, p.platform_id FROM posts p JOIN channels c ON c.id = p.channel_id "
        "WHERE p.state = 'uploaded_private' AND c.platform = 'youtube' "
        "AND p.platform_id IS NOT NULL AND p.platform_id <> ? ORDER BY p.id",
        (MISSING,),
    ).fetchall()
    for row in rows:
        try:
            public = visibility.is_public(row["platform_id"])
        except VisibilityError as exc:
            report.warnings.append(f"post {row['id']}: {exc}")
            continue
        if public:
            _write(conn, row["id"], {"state": "published", "published_at": stamp(now)}, now)
            report.changes.append(Change(row["id"], "uploaded_private", "published"))


def reject(conn: sqlite3.Connection, post_id: int, reason: str, now: datetime) -> str:
    """`ce reject <post_id> "<why>"`: Abbas's reason, which the retro reads (§10). Returns
    the state the post was in. The engine deletes nothing in Postiz or YouTube."""
    reason = " ".join(str(reason).split())
    if not reason:
        raise ReconcileError('give a reason: ce reject <post_id> "<why>"')
    row = conn.execute("SELECT state FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        raise ReconcileError(f"no post {post_id}")
    if row["state"] not in REJECTABLE:
        raise ReconcileError(
            f"post {post_id} is {row['state']}; only {', '.join(REJECTABLE)} posts can be rejected"
        )
    conn.execute(
        "UPDATE posts SET state = 'rejected', reject_reason = ?, updated_at = ? WHERE id = ?",
        (reason[:REASON_MAX], stamp(now), post_id),
    )
    return row["state"]
