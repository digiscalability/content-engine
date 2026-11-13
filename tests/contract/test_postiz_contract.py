"""Publisher contract (FR-PUB-1, 2, 4, §10, NFR-8): the Postiz CLI adapter (on a fake runner
that prints what the real CLI prints) and FakePostiz pass the same tests."""

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine.publish.postiz import (
    POSTIZ_VERSION,
    ChannelSettings,
    FakePostiz,
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
