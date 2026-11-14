"""Whether a YouTube video is public, for the private route of FR-PUB-3 (C15, ADR 0021).

Avatar posts go up private through Postiz; Abbas ticks "altered or synthetic content" in
YouTube Studio and makes the video public (or schedules it there). Postiz cannot see that
change, so reconciliation asks YouTube's oEmbed endpoint, which needs no key or account:
it answers 200 for a video anyone can watch and refuses a private or unknown one (400, 401,
403 or 404). Only a 200 counts as public; anything else unexpected is an error, so a post
is never marked live on a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import httpx

OEMBED = "https://www.youtube.com/oembed"
WATCH = "https://www.youtube.com/watch?v="
NOT_PUBLIC = (400, 401, 403, 404)
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


class VisibilityError(RuntimeError):
    """YouTube's answer said neither public nor not public."""


@runtime_checkable
class Visibility(Protocol):
    def is_public(self, video_id: str) -> bool: ...


def _check_id(video_id: str) -> None:
    if not _VIDEO_ID.fullmatch(video_id or ""):
        raise VisibilityError(f"not a YouTube video id: {video_id!r}")


@dataclass
class OEmbedVisibility:
    transport: httpx.BaseTransport | None = None
    timeout_s: float = 20.0

    def is_public(self, video_id: str) -> bool:
        _check_id(video_id)
        params = {"url": WATCH + video_id, "format": "json"}
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout_s) as client:
                response = client.get(OEMBED, params=params)
        except httpx.HTTPError as exc:
            raise VisibilityError(f"YouTube oEmbed for {video_id} failed: {exc}") from exc
        if response.status_code == 200:
            return True
        if response.status_code in NOT_PUBLIC:
            return False
        raise VisibilityError(f"YouTube oEmbed for {video_id} answered {response.status_code}")


@dataclass
class FakeVisibility:
    public: set[str] = field(default_factory=set)

    def is_public(self, video_id: str) -> bool:
        _check_id(video_id)
        return video_id in self.public
