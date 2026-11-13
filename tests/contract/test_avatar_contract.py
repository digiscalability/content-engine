"""AvatarProvider contract (FR-PRD-3: "both routes pass the same contract test", NFR-8).

HeyGen runs against a mocked v3 API; the drop folder against a temp inbox where the test
plays Abbas dropping the rendered file. Route-specific tests pin each route's details.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from engine.produce.avatar import (
    AvatarError,
    AvatarProvider,
    AvatarRequest,
    DropFolderAvatar,
    HeyGenAvatar,
)

KEY = "hg-" + "k" * 24
START = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class FakeHeyGen:
    """api.heygen.com v3: assets, video create and status, plus the video download."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.videos: dict[str, dict] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "files.heygen.test":
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        if request.headers.get("x-api-key") != KEY:
            return httpx.Response(401, json={"error": "unauthorized"})
        if path == "/v3/assets":
            return httpx.Response(200, json={"data": {"asset_id": "asset-1", "url": "https://x"}})
        if path == "/v3/videos" and request.method == "POST":
            video_id = f"vid-{len(self.videos) + 1}"
            self.videos[video_id] = {"video_id": video_id, "status": "pending"}
            return httpx.Response(202, json={"data": {"video_id": video_id, "status": "pending"}})
        if path.startswith("/v3/videos/"):
            return httpx.Response(200, json={"data": self.videos[path.rsplit("/", 1)[1]]})
        return httpx.Response(404)

    def complete(self):
        for video in self.videos.values():
            video.update(
                status="completed", video_url=f"https://files.heygen.test/{video['video_id']}.mp4"
            )

    def fail(self, message="Audio too long for avatar"):
        for video in self.videos.values():
            video.update(status="failed", failure_code="AUDIO_TOO_LONG", failure_message=message)

    def creates(self) -> int:
        return sum(1 for r in self.requests if r.url.path == "/v3/videos" and r.method == "POST")


def make_request(tmp_path: Path) -> AvatarRequest:
    audio = tmp_path / "work" / "script-7.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"RIFF....WAVE")
    return AvatarRequest(
        brief_id=17, audio=audio, avatar_id="abbas-v1", script="Discipline is a skill."
    )


@pytest.fixture(params=["heygen", "drop_folder"])
def route(request, tmp_path):
    clock, inbox = Clock(), tmp_path / "inbox" / "avatar"
    if request.param == "heygen":
        server = FakeHeyGen()
        provider = HeyGenAvatar(
            api_key=KEY, inbox=inbox, transport=httpx.MockTransport(server), now=clock
        )
        complete = server.complete
    else:
        server = None
        provider = DropFolderAvatar(inbox=inbox, now=clock)

        def complete():
            (inbox / "17.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)

    return provider, complete, clock, inbox, server


def test_is_an_avatar_provider(route):
    assert isinstance(route[0], AvatarProvider)


def test_request_starts_waiting(route, tmp_path):
    provider, *_ = route
    status = provider.request(make_request(tmp_path))
    assert status.state == "waiting" and status.video is None


def test_poll_waits_until_the_video_exists(route, tmp_path):
    provider, *_ = route
    req = make_request(tmp_path)
    provider.request(req)
    assert provider.poll(req).state == "waiting"


def test_ready_video_lands_in_the_inbox(route, tmp_path):
    provider, complete, _, inbox, _ = route
    req = make_request(tmp_path)
    provider.request(req)
    complete()
    status = provider.poll(req)
    assert status.state == "ready"
    assert status.video == inbox / "17.mp4"
    assert status.video.stat().st_size > 0


def test_request_again_resumes_instead_of_resubmitting(route, tmp_path):
    provider, complete, _, _, server = route
    req = make_request(tmp_path)
    provider.request(req)
    assert provider.request(req).state == "waiting"
    complete()
    assert provider.request(req).state == "ready"
    if server is not None:
        assert server.creates() == 1


def test_request_expires_after_48_hours(route, tmp_path):
    provider, _, clock, _, _ = route
    req = make_request(tmp_path)
    provider.request(req)
    clock.now = START + timedelta(hours=47, minutes=59)
    assert provider.poll(req).state == "waiting"
    clock.now = START + timedelta(hours=48, minutes=1)
    assert provider.poll(req).state == "expired"


def test_heygen_sends_audio_avatar_and_portrait_format(tmp_path):
    server = FakeHeyGen()
    provider = HeyGenAvatar(
        api_key=KEY, inbox=tmp_path / "inbox", transport=httpx.MockTransport(server)
    )
    provider.request(make_request(tmp_path))
    upload, create = server.requests
    assert upload.url.path == "/v3/assets" and b'name="file"' in upload.content
    body = json.loads(create.content)
    assert body == {
        "type": "avatar",
        "avatar_id": "abbas-v1",
        "audio_asset_id": "asset-1",
        "aspect_ratio": "9:16",
        "resolution": "1080p",
        "title": "brief 17",
    }


def test_failed_job_reports_the_reason(tmp_path):
    server = FakeHeyGen()
    provider = HeyGenAvatar(
        api_key=KEY, inbox=tmp_path / "inbox", transport=httpx.MockTransport(server)
    )
    req = make_request(tmp_path)
    provider.request(req)
    server.fail()
    status = provider.poll(req)
    assert status.state == "failed" and "Audio too long" in status.detail


def test_heygen_errors_never_show_the_key(tmp_path):
    provider = HeyGenAvatar(
        api_key="wrong-" + "z" * 20,
        inbox=tmp_path / "inbox",
        transport=httpx.MockTransport(FakeHeyGen()),
    )
    with pytest.raises(AvatarError, match="401") as exc:
        provider.request(make_request(tmp_path))
    assert "zzzz" not in str(exc.value)


def test_drop_folder_writes_a_request_card(tmp_path):
    inbox = tmp_path / "inbox"
    status = DropFolderAvatar(inbox=inbox, now=Clock()).request(make_request(tmp_path))
    for part in ("abbas-v1", "script-7.wav", "Discipline is a skill.", str(inbox / "17.mp4")):
        assert part in status.detail
    job = json.loads((inbox / "17.job.json").read_text(encoding="utf-8"))
    assert job["route"] == "drop_folder" and job["requested_at"] == START.isoformat()


def test_drop_folder_ignores_an_empty_file_still_being_copied(tmp_path):
    inbox = tmp_path / "inbox"
    provider = DropFolderAvatar(inbox=inbox, now=Clock())
    req = make_request(tmp_path)
    provider.request(req)
    (inbox / "17.mp4").write_bytes(b"")
    assert provider.poll(req).state == "waiting"
