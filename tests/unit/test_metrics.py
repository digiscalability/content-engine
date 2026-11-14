"""The metrics pull (FR-LRN-1, NFR-1; ADR 0021): one snapshot per post at 24 h, 72 h and 7 d
after publish from `postiz analytics:post`, and `{"missing": true}` resolved through
`posts:missing` / `posts:connect` only when the match is unambiguous."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from postseed import row, seed_post, seeded_db

from engine.learn.metrics import due_slot, due_snapshots, pull_metrics
from engine.publish.postiz import FakePostiz, PostizError, draft_payload

PUB = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
VIDEO = "dQw4w9WgXcQ"


@dataclass
class Counting(FakePostiz):
    asked: list = field(default_factory=list)
    connects: list = field(default_factory=list)

    def post_analytics(self, post_id, days):
        self.asked.append((post_id, days))
        return super().post_analytics(post_id, days)

    def connect(self, post_id, release_id):
        self.connects.append((post_id, release_id))
        super().connect(post_id, release_id)


@pytest.fixture
def conn(tmp_path, repo_root):
    return seeded_db(tmp_path / "engine.db", repo_root)


@pytest.fixture
def postiz():
    return Counting()


def live(conn, postiz, platform="youtube", release=VIDEO, published=PUB, state="published"):
    integration = {"youtube": "yt-1", "tiktok": "tt-1"}[platform]
    pid = postiz.create_draft(draft_payload(integration, platform, "c", [], {}, published))
    if release == "missing":
        postiz.publish_missing(pid)
    else:
        postiz.publish(pid, "https://example.test/v", release)
    post = seed_post(conn, state, platform, postiz_id=pid, platform_id=release,
                     published_at=published.strftime("%Y-%m-%dT%H:%M:%SZ"))  # fmt: skip
    return post, pid


def snapshots(conn, post):
    return {
        r["at"]: r
        for r in conn.execute("SELECT * FROM metrics_snapshots WHERE post_id = ?", (post,))
    }


@pytest.mark.parametrize(
    ("age", "slot"),
    [
        (timedelta(hours=23, minutes=59), None),
        (timedelta(hours=24), "24h"),
        (timedelta(hours=71), "24h"),
        (timedelta(hours=72), "72h"),
        (timedelta(days=6, hours=23), "72h"),
        (timedelta(days=7), "7d"),
        (timedelta(days=14), "7d"),
        (timedelta(days=14, hours=1), None),
    ],
)
def test_the_due_slot_is_the_latest_one_passed(age, slot):
    assert due_slot(PUB, PUB + age) == slot


def test_a_24h_snapshot_stores_the_lifetime_counts(conn, postiz):
    post, pid = live(conn, postiz)
    postiz.set_metrics(pid, Views=120, Likes=9, Comments=2, Favorites=0)
    now = PUB + timedelta(hours=25)
    report = pull_metrics(conn, postiz, now)
    assert report.taken == [(post, "24h")]
    assert postiz.asked == [(pid, 1)]
    got = snapshots(conn, post)["24h"]
    assert (got["views"], got["likes"], got["comments"], got["shares"]) == (120, 9, 2, None)
    assert (got["watch_time"], got["followers_delta"], got["clicks"]) == (None, None, None)
    assert got["taken_at"] == "2026-11-21T10:00:00Z"


def test_each_slot_is_taken_once(conn, postiz):
    post, pid = live(conn, postiz)
    postiz.set_metrics(pid, Views=1)
    taken = []
    for hours in (25, 26, 73, 74, 169, 240, 400):
        taken += pull_metrics(conn, postiz, PUB + timedelta(hours=hours)).taken
    assert taken == [(post, "24h"), (post, "72h"), (post, "7d")]
    assert [days for _, days in postiz.asked] == [1, 3, 7]


def test_days_off_store_only_the_latest_slot(conn, postiz):
    post, pid = live(conn, postiz)
    postiz.set_metrics(pid, Views=500)
    assert pull_metrics(conn, postiz, PUB + timedelta(days=8)).taken == [(post, "7d")]
    assert pull_metrics(conn, postiz, PUB + timedelta(days=9)).taken == []
    assert set(snapshots(conn, post)) == {"7d"}


def test_only_published_posts_are_pulled(conn, postiz):
    live(conn, postiz, state="uploaded_private")
    seed_post(conn, "drafted", postiz_id="post-x")
    assert pull_metrics(conn, postiz, PUB + timedelta(days=2)).taken == []
    assert postiz.asked == []


def test_no_numbers_yet_writes_nothing_and_tries_again(conn, postiz):
    post, pid = live(conn, postiz)
    postiz.set_metrics(pid, Favorites=3)  # nothing the table stores
    report = pull_metrics(conn, postiz, PUB + timedelta(hours=25))
    assert (report.taken, report.empty) == ([], [post])
    postiz.set_metrics(pid, Views=12)
    assert pull_metrics(conn, postiz, PUB + timedelta(hours=31)).taken == [(post, "24h")]


def test_a_missing_release_is_connected_then_pulled(conn, postiz):
    post, pid = live(conn, postiz, platform="tiktok", release="missing")
    postiz.candidates["tt-1"] = [{"id": "7321", "url": "https://p/a.jpg"}]
    postiz.metrics_for_release["7321"] = {"Views": 40, "Shares": 3}
    report = pull_metrics(conn, postiz, PUB + timedelta(hours=25))
    assert report.connected == [(post, "7321")] and report.taken == [(post, "24h")]
    assert row(conn, post)["platform_id"] == "7321"
    got = snapshots(conn, post)["24h"]
    assert (got["views"], got["shares"]) == (40, 3)


def test_a_video_already_linked_is_not_a_candidate(conn, postiz):
    live(conn, postiz, platform="tiktok", release="7000", published=PUB - timedelta(days=1))
    post, _ = live(conn, postiz, platform="tiktok", release="missing")
    postiz.candidates["tt-1"] = [{"id": "7321", "url": "u"}, {"id": "7000", "url": "u"}]
    report = pull_metrics(conn, postiz, PUB + timedelta(hours=25))
    assert (post, "7321") in report.connected


def test_an_extra_video_on_the_account_connects_nothing(conn, postiz):
    post, _ = live(conn, postiz, platform="tiktok", release="missing")
    postiz.candidates["tt-1"] = [{"id": "7999", "url": "u"}, {"id": "7321", "url": "u"}]
    report = pull_metrics(conn, postiz, PUB + timedelta(hours=25))
    assert (report.connected, report.unresolved, report.taken) == ([], [post], [])
    assert postiz.connects == []
    assert row(conn, post)["platform_id"] == "missing"


def test_two_missing_posts_connect_newest_to_newest(conn, postiz):
    older, _ = live(conn, postiz, platform="tiktok", release="missing",
                    published=PUB - timedelta(hours=5))  # fmt: skip
    newer, _ = live(conn, postiz, platform="tiktok", release="missing")
    postiz.candidates["tt-1"] = [{"id": "8002", "url": "u"}, {"id": "8001", "url": "u"}]
    pull_metrics(conn, postiz, PUB + timedelta(hours=25))
    assert row(conn, newer)["platform_id"] == "8002"
    assert row(conn, older)["platform_id"] == "8001"


@dataclass
class FailsFor(Counting):
    bad: set = field(default_factory=set)

    def post_analytics(self, post_id, days):
        if post_id in self.bad:
            raise PostizError(f"postiz analytics:post failed (exit 1): 500 for {post_id}")
        return super().post_analytics(post_id, days)


def test_a_failing_pull_is_reported_and_the_rest_are_taken(conn):
    """Review fix: a Postiz error on one post no longer stops every later post."""
    postiz = FailsFor()
    first, bad = live(conn, postiz)
    second, good = live(conn, postiz, release="aaaaaaaaaaa")
    postiz.set_metrics(good, Views=5)
    postiz.bad = {bad}
    report = pull_metrics(conn, postiz, PUB + timedelta(hours=25))
    assert report.taken == [(second, "24h")]
    assert [post for post, _ in report.failed] == [first]
    assert "500" in report.failed[0][1]


def test_due_snapshots_lists_without_calling(conn, postiz):
    post, _ = live(conn, postiz)
    due = due_snapshots(conn, PUB + timedelta(hours=25))
    assert [(d.post_id, d.slot, d.days) for d in due] == [(post, "24h", 1)]
    assert postiz.asked == []
