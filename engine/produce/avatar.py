"""Avatar video for format C (SRD FR-PRD-3), behind one `AvatarProvider` interface.

Two routes pass the same contract test:

- `HeyGenAvatar`: uploads the script WAV, asks HeyGen's v3 API for a 9:16 1080p video of
  the avatar speaking it, and downloads the result when it is ready.
- `DropFolderAvatar`: writes a request card (for the daily digest) and waits for Abbas to
  render the video in HeyGen's web app and save it to the inbox.

Either way the video lands at `media/inbox/avatar/<brief_id>.mp4`, and a request older
than 48 h expires. Each request keeps a small job file beside it so a crashed run resumes
the same job instead of paying for a second one (NFR-1). Argil or D-ID can replace HeyGen
by implementing the same two methods.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

import httpx

from engine.produce.ffmpeg import wav_duration
from engine.produce.process import Runner, output_tail, run_command

HEYGEN_API = "https://api.heygen.com"
MAX_WAIT = timedelta(hours=48)  # FR-PRD-3

AvatarState = Literal["waiting", "ready", "failed", "expired"]


class AvatarError(RuntimeError):
    """The avatar provider refused or failed a call."""


@dataclass(frozen=True)
class AvatarRequest:
    brief_id: int
    audio: Path  # the voiced script, WAV 48 kHz
    avatar_id: str
    script: str


@dataclass(frozen=True)
class AvatarStatus:
    state: AvatarState
    video: Path | None = None
    detail: str = ""


@runtime_checkable
class AvatarProvider(Protocol):
    def request(self, req: AvatarRequest) -> AvatarStatus:
        """Start (or resume) the avatar job for a brief."""
        ...

    def poll(self, req: AvatarRequest) -> AvatarStatus:
        """Where the job stands; `ready` once the MP4 is in the inbox."""
        ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Inbox:
    inbox: Path
    now: Callable[[], datetime]
    max_wait: timedelta

    def video(self, req: AvatarRequest) -> Path:
        return self.inbox / f"{req.brief_id}.mp4"

    def job_path(self, req: AvatarRequest) -> Path:
        return self.inbox / f"{req.brief_id}.job.json"

    def job(self, req: AvatarRequest) -> dict | None:
        path = self.job_path(req)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def save_job(self, req: AvatarRequest, **fields) -> dict:
        self.inbox.mkdir(parents=True, exist_ok=True)
        job = {
            "brief_id": req.brief_id,
            "avatar_id": req.avatar_id,
            "audio": str(req.audio),
            "requested_at": self.now().isoformat(),
            **fields,
        }
        self.job_path(req).write_text(json.dumps(job, indent=1), encoding="utf-8")
        return job

    def ready(self, req: AvatarRequest) -> AvatarStatus | None:
        video = self.video(req)
        if video.is_file() and video.stat().st_size > 0:
            return AvatarStatus("ready", video)
        return None

    def expired(self, job: dict) -> bool:
        return self.now() - datetime.fromisoformat(job["requested_at"]) > self.max_wait


@dataclass
class DropFolderAvatar:
    inbox: Path
    max_wait: timedelta = MAX_WAIT
    now: Callable[[], datetime] = _utcnow

    def __post_init__(self) -> None:
        self._box = _Inbox(self.inbox, self.now, self.max_wait)

    def card(self, req: AvatarRequest) -> str:
        return (
            f"Avatar request for brief {req.brief_id}: in HeyGen, make a 9:16 1080p video of "
            f"avatar '{req.avatar_id}' speaking {req.audio} (script: \"{req.script}\") and save "
            f"it as {self._box.video(req)}. It expires {self.max_wait} after the request."
        )

    def request(self, req: AvatarRequest) -> AvatarStatus:
        if self._box.job(req) is None and self._box.ready(req) is None:
            self._box.save_job(req, route="drop_folder", script=req.script)
        return self.poll(req)

    def poll(self, req: AvatarRequest) -> AvatarStatus:
        if ready := self._box.ready(req):
            return ready
        job = self._box.job(req)
        if job is None:
            return AvatarStatus("failed", detail=f"brief {req.brief_id} was never requested")
        if self._box.expired(job):
            return AvatarStatus("expired", detail=f"no video after {self.max_wait}")
        return AvatarStatus("waiting", detail=self.card(req))


@dataclass
class HeyGenAvatar:
    api_key: str
    inbox: Path
    transport: httpx.BaseTransport | None = None
    base_url: str = HEYGEN_API
    max_wait: timedelta = MAX_WAIT
    now: Callable[[], datetime] = _utcnow

    def __post_init__(self) -> None:
        self._box = _Inbox(self.inbox, self.now, self.max_wait)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"x-api-key": self.api_key},
            transport=self.transport,
            timeout=120,
        )

    @staticmethod
    def _data(response: httpx.Response, what: str) -> dict:
        if response.status_code not in (200, 201, 202):
            raise AvatarError(
                f"HeyGen {what} failed ({response.status_code}): {response.text[:300]}"
            )
        body = response.json()
        return body.get("data", body)

    def request(self, req: AvatarRequest) -> AvatarStatus:
        if self._box.ready(req) is None and self._box.job(req) is None:
            with self._client() as client:
                files = {"file": (req.audio.name, req.audio.read_bytes(), "audio/wav")}
                asset = self._data(client.post("/v3/assets", files=files), "audio upload")
                body = {
                    "type": "avatar",
                    "avatar_id": req.avatar_id,
                    "audio_asset_id": asset["asset_id"],
                    "aspect_ratio": "9:16",
                    "resolution": "1080p",
                    "title": f"brief {req.brief_id}",
                }
                video = self._data(client.post("/v3/videos", json=body), "video create")
            self._box.save_job(req, route="heygen", video_id=video["video_id"])
            return AvatarStatus("waiting", detail=f"HeyGen video {video['video_id']} queued")
        return self.poll(req)

    def poll(self, req: AvatarRequest) -> AvatarStatus:
        if ready := self._box.ready(req):
            return ready
        job = self._box.job(req)
        if job is None:
            return AvatarStatus("failed", detail=f"brief {req.brief_id} was never requested")
        if self._box.expired(job):
            return AvatarStatus("expired", detail=f"HeyGen video not ready after {self.max_wait}")
        with self._client() as client:
            video = self._data(client.get(f"/v3/videos/{job['video_id']}"), "video status")
        if video["status"] == "failed":
            return AvatarStatus("failed", detail=video.get("failure_message", "HeyGen failed"))
        if video["status"] != "completed":
            return AvatarStatus("waiting", detail=f"HeyGen video {video['status']}")
        self._download(video["video_url"], self._box.video(req))
        return AvatarStatus("ready", self._box.video(req))

    def _download(self, url: str, target: Path) -> None:
        # a presigned URL on another host: no API key on this request
        with httpx.Client(transport=self.transport, timeout=300) as client:
            response = client.get(url)
        if response.status_code != 200:
            raise AvatarError(f"HeyGen video download failed ({response.status_code})")
        partial = target.with_suffix(".mp4.part")
        partial.write_bytes(response.content)
        partial.replace(target)


# A placeholder presenter: a head-and-shoulders card in slate, 9:16 at 30 fps.
CARD = (
    "color=c=0x334155:s=1080x1920:r=30:d={seconds},"
    "drawbox=x=360:y=520:w=360:h=420:color=0xcbd5e1:t=fill,"
    "drawbox=x=240:y=980:w=600:h=560:color=0x94a3b8:t=fill"
)


@dataclass
class FakeAvatar:
    """Dry runs and tests: a card video as long as the voice, made at once with ffmpeg, in
    the same inbox layout as the real routes. `ready=False` leaves requests waiting, as
    the drop folder does until Abbas saves the file."""

    inbox: Path
    ready: bool = True
    run: Runner = run_command
    requests: list[AvatarRequest] = field(default_factory=list)

    def _video(self, req: AvatarRequest) -> Path:
        return self.inbox / f"{req.brief_id}.mp4"

    def request(self, req: AvatarRequest) -> AvatarStatus:
        self.requests.append(req)
        video = self._video(req)
        if self.ready and not video.is_file():
            self._render(req, video)
        return self.poll(req)

    def poll(self, req: AvatarRequest) -> AvatarStatus:
        video = self._video(req)
        if video.is_file() and video.stat().st_size > 0:
            return AvatarStatus("ready", video)
        return AvatarStatus("waiting", detail=f"dry run: avatar for brief {req.brief_id} waits")

    def _render(self, req: AvatarRequest, video: Path) -> None:
        seconds = f"{wav_duration(req.audio):g}"
        self.inbox.mkdir(parents=True, exist_ok=True)
        argv = [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", CARD.format(seconds=seconds),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            str(video),
        ]  # fmt: skip
        result = self.run(argv, None)
        if result.returncode != 0:
            raise AvatarError(f"fake avatar render failed: {output_tail(result)}")
