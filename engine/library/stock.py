"""Stock footage behind `StockProvider` (SRD FR-ING-5, NFR-8). ADR 0023.

`PexelsStock` searches the Pexels video API for portrait clips and downloads one file of a
clip; `FakeStock` stands in for it in tests and dry runs. Only the nightly `ce stock fetch`
job calls a provider (`engine/library/stock_fetch.py`), and only for the beat queries a
scriptwriter asked for.

The key goes in one header on the API client and nowhere else: file downloads come from
Pexels' file host and carry no key. Errors name the status and the API's text, never the key.
A download is written to `<name>.part`, checked to be an MP4, then moved into place.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

PEXELS_API = "https://api.pexels.com"
FULL_HD_WIDTH = 1080  # the render is 1080x1920; stock is never upscaled


class StockError(RuntimeError):
    """The stock provider could not search or deliver a file."""


class StockRateLimited(StockError):
    """The provider's request limit is spent (Pexels: 200 an hour, 20,000 a month)."""


@dataclass(frozen=True)
class StockFile:
    id: str
    width: int
    height: int
    link: str
    file_type: str
    quality: str | None = None
    fps: float | None = None


@dataclass(frozen=True)
class StockVideo:
    id: str
    page_url: str  # the clip's page, linked in its credit
    duration: float
    width: int
    height: int
    creator: str
    creator_url: str
    files: tuple[StockFile, ...]


@runtime_checkable
class StockProvider(Protocol):
    name: str

    def search(self, query: str, per_page: int) -> list[StockVideo]: ...

    def download(self, file: StockFile, output: Path) -> Path: ...

    def credit(self, video: StockVideo) -> str: ...


def pick_file(video: StockVideo, min_width: int = FULL_HD_WIDTH) -> StockFile | None:
    """The narrowest portrait MP4 at least `min_width` wide; None if the clip has none."""
    fitting = [
        f for f in video.files
        if f.file_type == "video/mp4" and f.width and f.height
        and f.height > f.width and f.width >= min_width
    ]  # fmt: skip
    return min(fitting, key=lambda f: (f.width, f.height), default=None)


def _is_mp4(head: bytes) -> bool:
    return head[4:8] == b"ftyp"


def _credit(video: StockVideo) -> str:
    # Pexels' guideline form ("Photo by John Doe on Pexels"), for a video
    return f"Video by {video.creator} on Pexels"


@dataclass
class PexelsStock:
    api_key: str
    transport: httpx.BaseTransport | None = None
    base_url: str = PEXELS_API
    name: str = "pexels"

    @staticmethod
    def parse_video(data: dict) -> StockVideo:
        files = tuple(
            StockFile(
                id=str(f["id"]),
                width=int(f.get("width") or 0),
                height=int(f.get("height") or 0),
                link=str(f["link"]),
                file_type=str(f.get("file_type") or ""),
                quality=f.get("quality"),
                fps=f.get("fps"),
            )  # fmt: skip
            for f in data.get("video_files") or []
        )
        user = data.get("user") or {}
        return StockVideo(
            id=str(data["id"]), page_url=str(data["url"]), duration=float(data["duration"]),
            width=int(data["width"]), height=int(data["height"]),
            creator=str(user.get("name") or ""), creator_url=str(user.get("url") or ""),
            files=files,
        )  # fmt: skip

    def search(self, query: str, per_page: int) -> list[StockVideo]:
        params = {"query": query, "orientation": "portrait", "size": "medium",
                  "per_page": str(per_page)}  # fmt: skip
        with httpx.Client(base_url=self.base_url, headers={"Authorization": self.api_key},
                          transport=self.transport, timeout=60) as client:  # fmt: skip
            response = client.get("/v1/videos/search", params=params)
        self._check(response, f"search for {query!r}")
        return [self.parse_video(v) for v in response.json().get("videos") or []]

    def download(self, file: StockFile, output: Path) -> Path:
        if not file.link.startswith("https://"):
            raise StockError(f"Pexels file {file.id} has a link that is not https")
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(output.name + ".part")
        try:
            with (
                httpx.Client(transport=self.transport, timeout=300, follow_redirects=True) as c,
                c.stream("GET", file.link) as response,
            ):
                if response.status_code != 200:
                    response.read()
                    self._check(response, f"download of file {file.id}")
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
            with partial.open("rb") as handle:
                if not _is_mp4(handle.read(12)):
                    raise StockError(f"Pexels file {file.id} is not an MP4")
            partial.replace(output)
        finally:
            partial.unlink(missing_ok=True)
        return output

    def credit(self, video: StockVideo) -> str:
        return _credit(video)

    @staticmethod
    def _check(response: httpx.Response, what: str) -> None:
        if response.status_code == 429:
            raise StockRateLimited(f"Pexels {what}: rate limit reached (429)")
        if response.status_code != 200:
            raise StockError(f"Pexels {what} failed ({response.status_code}): "
                             f"{response.text[:300]}")  # fmt: skip


@dataclass(frozen=True)
class FakeStockEntry:
    video: StockVideo
    keywords: frozenset[str]
    source: Path  # the file a download copies


@dataclass
class FakeStock:
    """Stands in for Pexels in tests and dry runs: a fixed catalogue, matched on any query
    word, whose downloads copy local fixture clips."""

    catalogue: Sequence[FakeStockEntry]
    name: str = "fake-pexels"
    searches: list[str] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)

    def search(self, query: str, per_page: int) -> list[StockVideo]:
        self.searches.append(query)
        words = set(query.lower().split())
        return [e.video for e in self.catalogue if words & e.keywords][:per_page]

    def download(self, file: StockFile, output: Path) -> Path:
        entry = next(e for e in self.catalogue if file in e.video.files)
        self.downloads.append(entry.video.id)
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(output.name + ".part")
        shutil.copyfile(entry.source, partial)
        partial.replace(output)
        return output

    def credit(self, video: StockVideo) -> str:
        return _credit(video)
