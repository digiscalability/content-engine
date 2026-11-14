"""Reconciliation (SRD §10 step 3, FR-PUB-3, NFR-1): every drafted post follows what Abbas
did in Postiz and YouTube Studio, and nothing is rejected on a partial view of Postiz."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from postseed import row, seed_post, seeded_db

from engine.learn.reconcile import (
    Change,
    ReconcileError,
    open_posts,
    reconcile,
    reject,
)
from engine.publish.postiz import FakePostiz, PostizError, draft_payload
from engine.publish.youtube import FakeVisibility, VisibilityError

NOW = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
SLOT = datetime(2026, 11, 19, 9, 30, tzinfo=UTC)
VIDEO = "dQw4w9WgXcQ"


@pytest.fixture
def conn(tmp_path, repo_root):
    return seeded_db(tmp_path / "engine.db", repo_root)


@pytest.fixture
def postiz():
    return FakePostiz()


def drafted(conn, postiz, platform="youtube", ai_disclosure=1, state="drafted", **fields):
    integration = {"youtube": "yt-1", "tiktok": "tt-1"}[platform]
    postiz_id = postiz.create_draft(draft_payload(integration, platform, "c", [], {}, SLOT))
    post = seed_post(conn, state, platform, ai_disclosure, postiz_id=postiz_id,
                     scheduled_at="2026-11-19T09:30:00Z", **fields)  # fmt: skip
    return post, postiz_id


def run(conn, postiz, public=(), now=NOW):
    return reconcile(conn, postiz, FakeVisibility(public=set(public)), now)


def test_scheduled_in_postiz_becomes_scheduled(conn, postiz):
    post, pid = drafted(conn, postiz)
    postiz.schedule(pid)
    report = run(conn, postiz)
    assert report.changes == [Change(post, "drafted", "scheduled")]
    assert row(conn, post)["scheduled_at"] == "2026-11-19T09:30:00Z"


def test_a_private_route_upload_waits_for_abbas_in_studio(conn, postiz):
    post, pid = drafted(conn, postiz)
    postiz.publish(pid, f"https://www.youtube.com/watch?v={VIDEO}", VIDEO)
    run(conn, postiz)
    got = row(conn, post)
    assert (got["state"], got["platform_id"], got["published_at"]) == (
        "uploaded_private",
        VIDEO,
        None,
    )


def test_made_public_in_studio_becomes_published_when_first_seen(conn, postiz):
    post, pid = drafted(conn, postiz)
    postiz.publish(pid, f"https://www.youtube.com/watch?v={VIDEO}", VIDEO)
    run(conn, postiz)
    report = run(conn, postiz, public={VIDEO})
    assert report.changes == [Change(post, "uploaded_private", "published")]
    assert row(conn, post)["published_at"] == "2026-11-20T08:00:00Z"


def test_a_public_route_post_is_published_at_its_publish_date(conn, postiz):
    post, pid = drafted(conn, postiz, platform="tiktok")
    postiz.publish(pid, "https://www.tiktok.com/@x/video/7321", "7321")
    run(conn, postiz)
    got = row(conn, post)
    assert (got["state"], got["published_at"], got["platform_id"]) == (
        "published",
        "2026-11-19T09:30:00Z",
        "7321",
    )


def test_a_tiktok_post_without_a_usable_id_is_kept_as_missing(conn, postiz):
    post, pid = drafted(conn, postiz, platform="tiktok")
    postiz.publish_missing(pid)
    run(conn, postiz)
    assert (row(conn, post)["state"], row(conn, post)["platform_id"]) == ("published", "missing")


def test_deleted_in_postiz_is_rejected(conn, postiz):
    kept, _ = drafted(conn, postiz)
    gone, pid = drafted(conn, postiz)
    postiz.delete(pid)
    report = run(conn, postiz)
    assert report.changes == [Change(gone, "drafted", "rejected")]
    assert row(conn, kept)["state"] == "drafted"
    assert row(conn, gone)["reject_reason"] is None  # Abbas may add one with ce reject


def test_a_failed_post_carries_the_postiz_error(conn, postiz):
    post, pid = drafted(conn, postiz)
    postiz.fail(pid, "token expired " + "x" * 400)
    run(conn, postiz)
    got = row(conn, post)
    assert got["state"] == "failed"
    assert got["reject_reason"].startswith("Postiz: token expired")
    assert len(got["reject_reason"]) <= 300


def test_a_rejected_post_that_reappears_is_restored(conn, postiz):
    post, _ = drafted(conn, postiz, state="rejected", updated_at="2026-11-19T00:00:00Z")
    report = run(conn, postiz)
    assert report.changes == [Change(post, "rejected", "drafted")]


def test_a_reason_from_abbas_is_final(conn, postiz):
    post, _ = drafted(conn, postiz, state="rejected", reject_reason="off-brand",
                      updated_at="2026-11-19T00:00:00Z")  # fmt: skip
    assert run(conn, postiz).changes == []
    assert row(conn, post)["state"] == "rejected"


def test_a_published_post_stays_published_when_gone_from_postiz(conn, postiz):
    post, pid = drafted(conn, postiz, state="published", platform_id=None,
                        published_at="2026-11-19T09:30:00Z")  # fmt: skip
    drafted(conn, postiz)  # keeps the channel visible in the list
    postiz.delete(pid)
    run(conn, postiz)
    assert row(conn, post)["state"] == "published"


def test_a_channel_absent_from_the_list_rejects_nothing(conn, postiz):
    post, pid = drafted(conn, postiz)
    postiz.delete(pid)  # Postiz now lists nothing for yt-1, as when a channel is disconnected
    report = run(conn, postiz)
    assert report.changes == []
    assert row(conn, post)["state"] == "drafted"
    assert any("yt-1" in w for w in report.warnings)


def test_a_rerun_changes_nothing(conn, postiz):
    post, pid = drafted(conn, postiz)
    postiz.publish(pid, "u", VIDEO)
    first = run(conn, postiz, public={VIDEO})
    assert [c.after for c in first.changes] == ["uploaded_private", "published"]
    before = dict(row(conn, post))
    assert run(conn, postiz, public={VIDEO}, now=NOW + timedelta(hours=6)).changes == []
    assert dict(row(conn, post)) == before


class Broken(FakePostiz):
    def list_posts(self, start, end):
        raise PostizError("postiz posts:list failed (exit 1): 401")


def test_a_postiz_failure_changes_nothing(conn):
    postiz = Broken()
    post, _ = drafted(conn, postiz)
    with pytest.raises(PostizError):
        run(conn, postiz)
    assert row(conn, post)["state"] == "drafted"


@dataclass
class Counting(FakePostiz):
    windows: list = field(default_factory=list)

    def list_posts(self, start, end):
        self.windows.append((start, end))
        return super().list_posts(start, end)


def test_no_open_post_means_no_postiz_call(conn):
    postiz = Counting()
    assert run(conn, postiz).changes == []
    assert postiz.windows == []


def test_the_window_covers_the_oldest_open_post(conn):
    postiz = Counting()
    drafted(conn, postiz, created_at="2026-10-01T00:00:00Z")
    run(conn, postiz)
    ((start, end),) = postiz.windows
    assert start <= datetime(2026, 9, 1, tzinfo=UTC)
    assert end >= NOW + timedelta(days=365)


class Flaky(FakeVisibility):
    def is_public(self, video_id):
        raise VisibilityError("YouTube oEmbed answered 503")


def test_a_youtube_error_leaves_the_post_waiting(conn, postiz):
    post, pid = drafted(conn, postiz)
    postiz.publish(pid, "u", VIDEO)
    report = reconcile(conn, postiz, Flaky(), NOW)
    assert row(conn, post)["state"] == "uploaded_private"
    assert any("503" in w for w in report.warnings)


def test_open_posts_lists_what_a_run_checks(conn, postiz):
    waiting, _ = drafted(conn, postiz)
    private, _ = drafted(conn, postiz, state="uploaded_private", platform_id=VIDEO)
    seed_post(conn, "qa_passed")
    assert [p.post_id for p in open_posts(conn, NOW)] == [waiting, private]


def test_reject_records_why(conn, postiz):
    post, _ = drafted(conn, postiz)
    assert reject(conn, post, "hook too slow", NOW) == "drafted"
    got = row(conn, post)
    assert (got["state"], got["reject_reason"]) == ("rejected", "hook too slow")


def test_reject_refuses_a_post_that_was_never_drafted(conn):
    post = seed_post(conn, "qa_passed")
    with pytest.raises(ReconcileError, match="qa_passed"):
        reject(conn, post, "no", NOW)
    with pytest.raises(ReconcileError, match="no post 999"):
        reject(conn, 999, "no", NOW)
    with pytest.raises(ReconcileError, match="reason"):
        reject(conn, post, "  ", NOW)
