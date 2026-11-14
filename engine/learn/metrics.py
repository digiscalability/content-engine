"""`ce metrics` (FR-LRN-1, NFR-1; ADR 0021): a snapshot of each published post at 24 h,
72 h and 7 d after publish, from `postiz analytics:post`.

- Postiz returns lifetime totals as of the pull, so each run stores only the latest slot that
  has come due. A post first reached at day 8 (the PC was off) gets a 7 d row and no 24 h or
  72 h copies of the same numbers; `taken_at` says when the numbers were read.
- Posts are pulled until 14 days after publish. A pull with none of the stored counts
  (views, likes, comments, shares) writes nothing and is tried again on the next run.
- `{"missing": true}`: the platform gave Postiz no usable id. `posts:missing` lists the
  channel's recent videos; those not already linked to a post are matched newest to newest
  with the channel's unlinked posts, and only when the two counts agree, so a video posted
  outside the engine never gets a post's numbers. Otherwise the digest asks Abbas to
  connect the post in Postiz.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from engine.learn.reconcile import parse, stamp
from engine.publish.postiz import MISSING, AnalyticsResult, PostizAnalytics, PostizError

SLOTS = (("24h", timedelta(hours=24), 1), ("72h", timedelta(hours=72), 3),
         ("7d", timedelta(days=7), 7))  # fmt: skip
PULL_UNTIL = timedelta(days=14)
STORED = ("views", "likes", "comments", "shares")  # Postiz labels, lowercased = columns


@dataclass(frozen=True)
class DueSnapshot:
    post_id: int
    postiz_id: str
    channel_id: int
    slot: str
    days: int


@dataclass
class MetricsReport:
    taken: list[tuple[int, str]] = field(default_factory=list)
    connected: list[tuple[int, str]] = field(default_factory=list)
    unresolved: list[int] = field(default_factory=list)
    empty: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)  # Postiz errors, per post


def due_slot(published_at: datetime, now: datetime) -> str | None:
    age = now - published_at
    if age > PULL_UNTIL:
        return None
    due = [name for name, after, _ in SLOTS if age >= after]
    return due[-1] if due else None


def due_snapshots(conn: sqlite3.Connection, now: datetime) -> list[DueSnapshot]:
    """The snapshots a run would take now, in post order."""
    rows = conn.execute(
        "SELECT id, postiz_id, channel_id, published_at FROM posts WHERE state = 'published' "
        "AND postiz_id IS NOT NULL AND published_at IS NOT NULL ORDER BY id"
    ).fetchall()
    due = []
    for r in rows:
        slot = due_slot(parse(r["published_at"]), now)
        if (
            slot is None
            or conn.execute(
                "SELECT 1 FROM metrics_snapshots WHERE post_id = ? AND at = ?", (r["id"], slot)
            ).fetchone()
        ):
            continue
        days = next(d for name, _, d in SLOTS if name == slot)
        due.append(DueSnapshot(r["id"], r["postiz_id"], r["channel_id"], slot, days))
    return due


def _connect_missing(
    conn: sqlite3.Connection, analytics: PostizAnalytics, due: DueSnapshot
) -> list[tuple[int, str]]:
    """Match the channel's unlinked posts to its unlinked videos when the counts agree."""
    linked = {
        pid
        for (pid,) in conn.execute(
            "SELECT platform_id FROM posts WHERE platform_id IS NOT NULL AND platform_id <> ?",
            (MISSING,),
        )
    }
    free = [c for c in analytics.missing_content(due.postiz_id) if c.release_id not in linked]
    waiting = conn.execute(
        "SELECT id, postiz_id FROM posts WHERE channel_id = ? AND state = 'published' "
        "AND platform_id = ? ORDER BY published_at DESC, id DESC",
        (due.channel_id, MISSING),
    ).fetchall()
    if not free or len(free) != len(waiting):
        return []
    pairs = []
    for post, candidate in zip(waiting, free, strict=True):  # both newest first
        analytics.connect(post["postiz_id"], candidate.release_id)
        conn.execute(
            "UPDATE posts SET platform_id = ? WHERE id = ?", (candidate.release_id, post["id"])
        )
        pairs.append((post["id"], candidate.release_id))
    return pairs


def _resolve(
    conn: sqlite3.Connection, analytics: PostizAnalytics, due: DueSnapshot,
    report: MetricsReport,
) -> AnalyticsResult | None:  # fmt: skip
    conn.execute("UPDATE posts SET platform_id = ? WHERE id = ?", (MISSING, due.post_id))
    pairs = _connect_missing(conn, analytics, due)
    report.connected += pairs
    if due.post_id not in {post for post, _ in pairs}:
        return None
    result = analytics.post_analytics(due.postiz_id, due.days)
    return None if result.missing else result


def pull_metrics(
    conn: sqlite3.Connection, analytics: PostizAnalytics, now: datetime
) -> MetricsReport:
    """Take every due snapshot. A Postiz error on one post is reported in `failed` and the
    run goes on, so one broken post never blocks the others (each row commits on its own)."""
    report = MetricsReport()
    for due in due_snapshots(conn, now):
        try:
            result = analytics.post_analytics(due.postiz_id, due.days)
            if result.missing:
                result = _resolve(conn, analytics, due, report)
        except PostizError as exc:
            report.failed.append((due.post_id, str(exc)))
            continue
        if result is None:
            report.unresolved.append(due.post_id)
            continue
        counts = [result.totals.get(name) for name in STORED]
        if all(count is None for count in counts):
            report.empty.append(due.post_id)
            continue
        conn.execute(
            "INSERT OR IGNORE INTO metrics_snapshots "
            "(post_id, at, views, likes, comments, shares, taken_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (due.post_id, due.slot, *counts, stamp(now)),
        )
        report.taken.append((due.post_id, due.slot))
    return report
