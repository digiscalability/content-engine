"""Postiz posting rules (FR-PUB-1..4, FR-PUB-3 routes, §10 states) as pure functions."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from engine.publish.postiz import (
    MISSING,
    AnalyticsResult,
    FakePostiz,
    PostizError,
    RemotePost,
    _remote_from,
    draft_payload,
    draft_post,
    engine_state,
    parse_analytics,
    tiktok_settings,
    unsupported_settings,
    youtube_settings,
)

WHEN = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)


def remote(state: str) -> RemotePost:
    return RemotePost("p1", "yt-1", state, WHEN, None, None)


def test_youtube_avatar_posts_go_up_private():
    assert youtube_settings("One habit", ai_disclosure=True) == {
        "title": "One habit",
        "type": "private",
        "selfDeclaredMadeForKids": "no",
    }
    assert youtube_settings("One habit", ai_disclosure=False)["type"] == "public"


def test_youtube_title_is_cut_to_100_characters():
    assert len(youtube_settings("x" * 140, ai_disclosure=True)["title"]) == 100


def test_tiktok_posts_directly_with_the_ai_label():
    settings = tiktok_settings(ai_disclosure=True)
    assert settings["content_posting_method"] == "DIRECT_POST"
    assert settings["video_made_with_ai"] is True
    assert settings["privacy_level"] == "PUBLIC_TO_EVERYONE"


def test_unknown_setting_is_refused():
    schema = {"properties": {"title": {}, "type": {}, "selfDeclaredMadeForKids": {}}}
    settings = {"title": "t", "type": "private", "containsSyntheticMedia": True}
    assert unsupported_settings(settings, schema) == ["containsSyntheticMedia"]
    assert unsupported_settings({"title": "t"}, {}) == []  # no schema: nothing to check


def test_draft_payload_shape():
    payload = draft_payload("yt-1", "youtube", "Caption", ["https://u/a.mp4"], {"title": "t"}, WHEN)
    assert payload["type"] == "draft"
    assert payload["shortLink"] is False
    assert payload["date"] == "2026-11-14T09:30:00Z"
    (post,) = payload["posts"]
    assert post["integration"] == {"id": "yt-1"}
    assert post["value"][0]["content"] == "Caption"
    assert post["value"][0]["image"][0]["path"] == "https://u/a.mp4"
    assert post["settings"] == {"__type": "youtube", "title": "t"}


@pytest.mark.parametrize(
    ("state", "private_route", "expected"),
    [
        ("DRAFT", False, "drafted"),
        ("QUEUE", False, "scheduled"),
        ("PUBLISHED", False, "published"),
        ("PUBLISHED", True, "uploaded_private"),
        ("ERROR", False, "failed"),
        (None, False, "rejected"),
    ],
)
def test_engine_state_mapping(state, private_route, expected):
    assert engine_state(remote(state) if state else None, private_route) == expected


def test_draft_post_uploads_first_then_drafts(tmp_path):
    video = tmp_path / "render.mp4"
    video.write_bytes(b"mp4")
    postiz = FakePostiz()
    post_id = draft_post(
        postiz, "yt-1", "youtube", "Caption", [video], youtube_settings("One habit", True), WHEN
    )
    (stored,) = postiz.list_posts(WHEN, WHEN)
    assert stored.post_id == post_id and stored.state == "DRAFT"
    assert postiz.uploads == [video]
    assert postiz.drafts[post_id]["posts"][0]["value"][0]["image"][0]["path"].endswith("render.mp4")


def test_draft_post_refuses_settings_postiz_would_drop(tmp_path):
    postiz = FakePostiz()
    bad = {**youtube_settings("One habit", True), "containsSyntheticMedia": True}
    with pytest.raises(PostizError, match="containsSyntheticMedia"):
        draft_post(postiz, "yt-1", "youtube", "Caption", [Path("never-read.mp4")], bad, WHEN)
    assert postiz.uploads == [] and postiz.drafts == {}


def test_analytics_take_the_last_point_of_each_label():
    data = [
        {"label": "Views", "percentageChange": 0,
         "data": [{"total": "10", "date": "2026-11-13"}, {"total": "1200", "date": "2026-11-14"}]},
        {"label": "Likes", "percentageChange": 0, "data": [{"total": "33.0", "date": "x"}]},
        {"label": "Reach", "percentageChange": 0, "data": [{"total": "n/a", "date": "x"}]},
        {"label": "Saves", "percentageChange": 0, "data": []},
    ]  # fmt: skip
    assert parse_analytics(data) == AnalyticsResult(False, {"views": 1200, "likes": 33})


def test_analytics_missing_and_empty():
    assert parse_analytics({"missing": True}) == AnalyticsResult(True, {})
    assert parse_analytics([]) == AnalyticsResult(False, {})


def test_remote_post_reads_the_release_id():
    post = _remote_from({"id": "p", "state": "PUBLISHED", "releaseId": "missing",
                         "integration": {"id": "tt-1"}})  # fmt: skip
    assert post.release_id == MISSING
