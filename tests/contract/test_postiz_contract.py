"""Publisher contract (FR-PUB-1, 2, 4, §10, NFR-8): the Postiz CLI adapter (on a fake runner
that prints what the real CLI prints) and FakePostiz pass the same tests."""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine.publish.postiz import (
    POSTIZ_VERSION,
    AnalyticsResult,
    ChannelSettings,
    FakePostiz,
    MissingCandidate,
    PostizAnalytics,
    PostizCLI,
    PostizError,
    Publisher,
    draft_payload,
)

KEY = "pz-" + "k" * 24
WHEN = datetime(2026, 11, 14, 9, 30, tzinfo=UTC)
NPX = "C:/Program Files/nodejs/npx.cmd"


class FakeCliRunner:
    """Plays the `postiz` CLI: an emoji header line, then pretty JSON, from a FakePostiz."""

    def __init__(self, backend: FakePostiz, fail: str | None = None):
        self.backend, self.fail = backend, fail
        self.calls: list[tuple[list[str], dict | None]] = []

    def __call__(self, argv, extra_env=None):
        self.calls.append((argv, extra_env))
        command, args = argv[3], argv[4:]
        if command == self.fail:
            return subprocess.CompletedProcess(
                argv, 1, "", '❌ Failed: API Error (401): {"msg":"Invalid API key"}'
            )
        if command == "upload":
            out = ("✅ File uploaded successfully!", self.backend.raw_upload(Path(args[0])))
        elif command == "integrations:settings":
            out = (f"⚙️  Settings for integration: {args[0]}", self.backend.raw_settings(args[0]))
        elif command == "posts:create":
            payload = json.loads(Path(args[args.index("--json") + 1]).read_text(encoding="utf-8"))
            out = ("✅ Post created successfully!", self.backend.raw_create(payload))
        elif command == "posts:list":
            out = ("📋 Posts:", self.backend.raw_list())
        elif command == "analytics:post":
            days = args[args.index("-d") + 1]
            out = (f"📊 Analytics for post: {args[0]}", self.backend.raw_analytics(args[0], days))
        elif command == "posts:missing":  # the real CLI prints no header line here
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(self.backend.raw_missing(args[0]), indent=2) + "\n", ""
            )
        elif command == "posts:connect":
            release = args[args.index("--release-id") + 1]
            out = (f"✅ Post {args[0]} connected to release {release}",
                   self.backend.raw_connect(args[0], release))  # fmt: skip
        else:
            return subprocess.CompletedProcess(argv, 1, "", f"unknown command {command}")
        return subprocess.CompletedProcess(
            argv, 0, f"{out[0]}\n{json.dumps(out[1], indent=2)}\n", ""
        )


@pytest.fixture(params=["cli", "fake"])
def publisher(request, tmp_path):
    backend = FakePostiz()
    if request.param == "fake":
        return backend, backend
    runner = FakeCliRunner(backend)
    cli = PostizCLI(
        api_key=KEY,
        work_dir=tmp_path / "postiz",
        run=runner,
        which=lambda name: NPX if name == "npx" else None,
    )
    return cli, backend


def video(tmp_path) -> Path:
    path = tmp_path / "renders" / "post 17.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"mp4")
    return path


def test_is_a_publisher(publisher):
    assert isinstance(publisher[0], Publisher)


def test_upload_returns_a_hosted_path(publisher, tmp_path):
    url = publisher[0].upload(video(tmp_path))
    assert url.startswith("https://") and url.endswith("post 17.mp4")


def test_channel_settings_carry_rules_and_schema(publisher):
    settings = publisher[0].channel_settings("yt-1")
    assert isinstance(settings, ChannelSettings)
    assert "private" in (settings.rules or "")
    assert "title" in settings.schema["properties"]


def test_a_draft_shows_in_the_list_as_draft(publisher, tmp_path):
    pub, _ = publisher
    url = pub.upload(video(tmp_path))
    post_id = pub.create_draft(
        draft_payload("yt-1", "youtube", "Caption", [url], {"title": "t"}, WHEN)
    )
    posts = {
        p.post_id: p for p in pub.list_posts(WHEN - timedelta(days=1), WHEN + timedelta(days=1))
    }
    assert posts[post_id].state == "DRAFT"
    assert posts[post_id].integration_id == "yt-1"


def test_list_follows_what_abbas_does_in_postiz(publisher, tmp_path):
    pub, backend = publisher
    window = (WHEN - timedelta(days=1), WHEN + timedelta(days=1))
    first = pub.create_draft(draft_payload("yt-1", "youtube", "a", [], {"title": "a"}, WHEN))
    second = pub.create_draft(draft_payload("yt-1", "youtube", "b", [], {"title": "b"}, WHEN))
    backend.schedule(first)
    backend.publish(first, "https://youtu.be/abc")
    backend.delete(second)
    posts = {p.post_id: p for p in pub.list_posts(*window)}
    assert posts[first].state == "PUBLISHED" and posts[first].release_url == "https://youtu.be/abc"
    assert second not in posts


def test_cli_runs_pinned_postiz_through_npx_with_the_key_in_env(tmp_path):
    runner = FakeCliRunner(FakePostiz())
    cli = PostizCLI(api_key=KEY, work_dir=tmp_path, run=runner, which=lambda n: NPX)
    cli.channel_settings("yt-1")
    ((argv, env),) = runner.calls
    assert argv[:4] == [NPX, "--yes", f"postiz@{POSTIZ_VERSION}", "integrations:settings"]
    assert env["POSTIZ_API_KEY"] == KEY
    assert env["npm_config_prefer_offline"] == "true"
    assert KEY not in " ".join(argv)


def test_upload_sends_a_forward_slash_path(tmp_path):
    runner = FakeCliRunner(FakePostiz())
    cli = PostizCLI(api_key=KEY, work_dir=tmp_path, run=runner, which=lambda n: NPX)
    path = video(tmp_path)
    cli.upload(path)
    sent = runner.calls[0][0][4]
    assert "\\" not in sent and sent == path.as_posix()


def test_draft_goes_as_a_json_file(tmp_path):
    runner = FakeCliRunner(FakePostiz())
    cli = PostizCLI(api_key=KEY, work_dir=tmp_path / "pz", run=runner, which=lambda n: NPX)
    payload = draft_payload("yt-1", "youtube", 'Say "hi" & <go>', [], {"title": "t"}, WHEN)
    cli.create_draft(payload)
    argv = runner.calls[0][0]
    assert argv[4] == "--json" and len(argv) == 6
    assert json.loads(Path(argv[5]).read_text(encoding="utf-8")) == payload


def test_cli_failure_raises_without_the_key(tmp_path):
    runner = FakeCliRunner(FakePostiz(), fail="posts:list")
    cli = PostizCLI(api_key=KEY, work_dir=tmp_path, run=runner, which=lambda n: NPX)
    with pytest.raises(PostizError, match="401") as exc:
        cli.list_posts(WHEN, WHEN)
    assert KEY not in str(exc.value)


def test_missing_npx_is_reported(tmp_path):
    cli = PostizCLI(
        api_key=KEY, work_dir=tmp_path, run=FakeCliRunner(FakePostiz()), which=lambda n: None
    )
    with pytest.raises(PostizError, match="Node"):
        cli.channel_settings("yt-1")


# Metrics and reconciliation (FR-LRN-1, §10) ------------------------------------------------


def published(pub, backend, release_id: str | None = "dQw4w9WgXcQ") -> str:
    post_id = pub.create_draft(draft_payload("yt-1", "youtube", "a", [], {"title": "a"}, WHEN))
    backend.schedule(post_id)
    backend.publish(post_id, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", release_id)
    return post_id


def test_is_postiz_analytics(publisher):
    assert isinstance(publisher[0], PostizAnalytics)


def test_list_carries_the_release_id(publisher):
    pub, backend = publisher
    post_id = published(pub, backend)
    (post,) = [p for p in pub.list_posts(WHEN, WHEN) if p.post_id == post_id]
    assert post.release_id == "dQw4w9WgXcQ"


def test_post_analytics_are_lifetime_totals_by_label(publisher):
    pub, backend = publisher
    post_id = published(pub, backend)
    backend.set_metrics(post_id, Views=120, Likes=9, Comments=2, Favorites=0)
    result = pub.post_analytics(post_id, 1)
    assert result == AnalyticsResult(
        missing=False, totals={"views": 120, "likes": 9, "comments": 2, "favorites": 0}
    )


def test_a_draft_has_no_analytics(publisher):
    pub, _ = publisher
    post_id = pub.create_draft(draft_payload("yt-1", "youtube", "a", [], {"title": "a"}, WHEN))
    assert pub.post_analytics(post_id, 7) == AnalyticsResult(missing=False, totals={})


def test_missing_release_is_found_and_connected(publisher):
    pub, backend = publisher
    post_id = pub.create_draft(draft_payload("tt-1", "tiktok", "a", [], {}, WHEN))
    backend.publish_missing(post_id)
    backend.candidates["tt-1"] = [{"id": "7321456789012345678", "url": "https://p/cover.jpg"}]
    backend.metrics_for_release["7321456789012345678"] = {"Views": 40, "Shares": 3}
    assert pub.post_analytics(post_id, 1) == AnalyticsResult(missing=True, totals={})
    assert pub.missing_content(post_id) == [
        MissingCandidate(release_id="7321456789012345678", url="https://p/cover.jpg")
    ]
    pub.connect(post_id, "7321456789012345678")
    assert pub.missing_content(post_id) == []
    assert pub.post_analytics(post_id, 1).totals == {"views": 40, "shares": 3}
    (post,) = [p for p in pub.list_posts(WHEN, WHEN) if p.post_id == post_id]
    assert post.release_id == "7321456789012345678"


def test_cli_analytics_and_connect_arguments(tmp_path):
    runner = FakeCliRunner(FakePostiz())
    cli = PostizCLI(api_key=KEY, work_dir=tmp_path, run=runner, which=lambda n: NPX)
    post_id = cli.create_draft(draft_payload("tt-1", "tiktok", "a", [], {}, WHEN))
    runner.backend.publish_missing(post_id)
    cli.post_analytics(post_id, 7)
    cli.missing_content(post_id)
    cli.connect(post_id, "7321")
    argvs = [argv[3:] for argv, _ in runner.calls[1:]]
    assert argvs == [
        ["analytics:post", post_id, "-d", "7"],
        ["posts:missing", post_id],
        ["posts:connect", post_id, "--release-id", "7321"],
    ]


BAD_IDS = ["7321&calc", "a|b", 'x"y', "a b", "^%PATH%", "", "a" * 65, "../x"]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_ids_from_postiz_never_reach_the_cmd_shim_unchecked(tmp_path, bad):
    """npx is a .cmd shim, so cmd.exe parses its arguments: an id carrying & or | from a
    Postiz or platform answer must be refused before any process starts (security review)."""
    runner = FakeCliRunner(FakePostiz())
    cli = PostizCLI(api_key=KEY, work_dir=tmp_path, run=runner, which=lambda n: NPX)
    calls = [
        lambda: cli.post_analytics(bad, 7),
        lambda: cli.missing_content(bad),
        lambda: cli.connect("post-1", bad),
        lambda: cli.connect(bad, "7321"),
        lambda: cli.channel_settings(bad),
    ]
    for call in calls:
        with pytest.raises(PostizError, match="unsafe"):
            call()
    assert runner.calls == []


def test_candidates_with_unsafe_ids_are_dropped(tmp_path):
    backend = FakePostiz()
    runner = FakeCliRunner(backend)
    cli = PostizCLI(api_key=KEY, work_dir=tmp_path, run=runner, which=lambda n: NPX)
    post_id = cli.create_draft(draft_payload("tt-1", "tiktok", "a", [], {}, WHEN))
    backend.publish_missing(post_id)
    backend.candidates["tt-1"] = [{"id": "7321&calc", "url": "u"}, {"id": "7321", "url": "u"}]
    assert [c.release_id for c in cli.missing_content(post_id)] == ["7321"]
