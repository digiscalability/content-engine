"""The dry run's last leg (NFR-8, §10, FR-LRN-1): fake Abbas schedules and publishes each
fake draft, and the real reconcile and metrics jobs carry it to `published` with a 24 h
snapshot, all inside the run's own DB."""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from postseed import row, seed_post

from engine.agents import dryrun
from engine.config import load_config
from engine.publish.postiz import draft_payload

NOW = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)


def fake_run(argv, env=None):
    Path(argv[-1]).write_bytes(f"clip {argv[-1]}".encode())
    return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def ctx(repo_root, tmp_path):
    return dryrun.start_dry_run(load_config(repo_root / "config"), tmp_path, "dry-1", run=fake_run)


def test_a_draft_is_followed_to_published_with_a_24h_snapshot(ctx):
    pid = ctx.providers.publisher.create_draft(draft_payload("yt-1", "youtube", "c", [], {}, NOW))
    post = seed_post(ctx.conn, "drafted", postiz_id=pid)
    done = dryrun.follow_up(ctx, NOW)
    assert done.states == {post: "published"}
    assert [(c.before, c.after) for c in done.changes] == [
        ("drafted", "uploaded_private"),
        ("uploaded_private", "published"),
    ]
    assert done.snapshots == [(post, "24h")]
    got = row(ctx.conn, post)
    assert got["platform_id"] == f"dryrun{post:05d}"
    views = ctx.conn.execute("SELECT views FROM metrics_snapshots WHERE post_id = ?", (post,))
    assert views.fetchone()[0] == dryrun.DRY_COUNTS["Views"]


def test_nothing_drafted_means_nothing_followed(ctx):
    done = dryrun.follow_up(ctx, NOW)
    assert (done.states, done.changes, done.snapshots) == ({}, [], [])
