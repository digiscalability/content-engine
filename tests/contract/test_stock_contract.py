"""StockProvider contract (FR-ING-5, NFR-8): Pexels on a mocked transport and FakeStock pass
the same tests; Pexels-only tests pin the request shape and the error handling. No real
Pexels call here: that one is `test_stock_live.py`, under the `live` marker."""

from __future__ import annotations

import httpx
import pytest

from engine.library.stock import (
    FakeStock,
    FakeStockEntry,
    PexelsStock,
    StockError,
    StockFile,
    StockProvider,
    StockRateLimited,
    StockVideo,
    pick_file,
)

KEY = "test-" + "p" * 30
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


def pexels_video(video_id: int = 857195, name: str = "Ana Lens") -> dict:
    """One video as the Pexels search endpoint returns it."""
    return {
        "id": video_id,
        "width": 1080,
        "height": 1920,
        "url": f"https://www.pexels.com/video/waves-on-the-shore-{video_id}/",
        "image": f"https://images.pexels.com/videos/{video_id}/pictures/preview-0.jpg",
        "duration": 14,
        "user": {"id": 42, "name": name, "url": "https://www.pexels.com/@ana-lens"},
        "video_files": [
            {"id": 1, "quality": "hd", "file_type": "video/mp4", "width": 2160,
             "height": 3840, "fps": 25, "link": f"https://videos.pexels.com/video-files/{video_id}/uhd.mp4"},
            {"id": 2, "quality": "hd", "file_type": "video/mp4", "width": 1080,
             "height": 1920, "fps": 25, "link": f"https://videos.pexels.com/video-files/{video_id}/hd.mp4"},
            {"id": 3, "quality": "sd", "file_type": "video/mp4", "width": 540,
             "height": 960, "fps": 25, "link": f"https://videos.pexels.com/video-files/{video_id}/sd.mp4"},
            {"id": 4, "quality": "hls", "file_type": "application/x-mpegURL", "width": None,
             "height": None, "fps": None, "link": f"https://videos.pexels.com/video-files/{video_id}/hls.m3u8"},
        ],
        "video_pictures": [],
    }  # fmt: skip


class FakePexelsServer:
    """Answers like api.pexels.com (search) and videos.pexels.com (files)."""

    def __init__(self, status: int = 200, body: bytes = MP4):
        self.status, self.body = status, body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text='{"error":"Unauthorized"}')
        if request.url.host == "api.pexels.com" and request.url.path == "/v1/videos/search":
            query = request.url.params["query"].lower()
            videos = [pexels_video()] if "wave" in query or "ocean" in query else []
            return httpx.Response(
                200,
                json={
                    "page": 1,
                    "per_page": 15,
                    "total_results": len(videos),
                    "url": "https://www.pexels.com/videos/",
                    "videos": videos,
                },  # fmt: skip
                headers={"X-Ratelimit-Limit": "20000", "X-Ratelimit-Remaining": "19999"},
            )
        if request.url.host == "videos.pexels.com":
            return httpx.Response(200, content=self.body)
        return httpx.Response(404)


def pexels(server: FakePexelsServer) -> PexelsStock:
    return PexelsStock(api_key=KEY, transport=httpx.MockTransport(server))


def fake_stock(tmp_path) -> FakeStock:
    source = tmp_path / "fixture.mp4"
    source.write_bytes(MP4)
    return FakeStock([FakeStockEntry(PexelsStock.parse_video(pexels_video()),
                                     frozenset({"wave", "waves", "ocean"}), source)])  # fmt: skip


@pytest.fixture(params=["pexels", "fake"])
def provider(request, tmp_path) -> StockProvider:
    if request.param == "pexels":
        return pexels(FakePexelsServer())
    return fake_stock(tmp_path)


def test_is_a_stock_provider(provider):
    assert isinstance(provider, StockProvider)


def test_search_returns_videos_with_their_licence_facts(provider):
    (video,) = provider.search("waves rolling on a blue ocean", per_page=5)
    assert isinstance(video, StockVideo)
    assert video.id == "857195"
    assert video.page_url == "https://www.pexels.com/video/waves-on-the-shore-857195/"
    assert (video.creator, video.creator_url) == ("Ana Lens", "https://www.pexels.com/@ana-lens")
    assert video.duration == 14
    assert provider.credit(video) == "Video by Ana Lens on Pexels"
    chosen = pick_file(video)
    assert chosen is not None and (chosen.width, chosen.height) == (1080, 1920)


def test_a_query_with_no_match_returns_nothing(provider):
    assert provider.search("a red sports car", per_page=5) == []


def test_download_writes_the_mp4_atomically(provider, tmp_path):
    (video,) = provider.search("ocean", per_page=1)
    out = provider.download(pick_file(video), tmp_path / "stock" / "pexels-857195.mp4")
    assert out.read_bytes()[4:8] == b"ftyp"
    assert not list(out.parent.glob("*.part"))


# Pexels only -------------------------------------------------------------------------------


def test_pexels_request_shape():
    server = FakePexelsServer()
    pexels(server).search("ocean waves", per_page=7)
    (request,) = server.requests
    assert (request.url.host, request.url.path) == ("api.pexels.com", "/v1/videos/search")
    assert request.headers["Authorization"] == KEY
    params = request.url.params
    assert (params["query"], params["orientation"], params["size"], params["per_page"]) == (
        "ocean waves", "portrait", "medium", "7")  # fmt: skip


def test_pexels_download_sends_no_key(tmp_path):
    server = FakePexelsServer()
    stock = pexels(server)
    (video,) = stock.search("ocean", per_page=1)
    stock.download(pick_file(video), tmp_path / "a.mp4")
    download = server.requests[-1]
    assert download.url.host == "videos.pexels.com"
    assert "Authorization" not in download.headers


def test_pexels_error_names_the_status_not_the_key():
    with pytest.raises(StockError) as exc:
        pexels(FakePexelsServer(status=401)).search("ocean", per_page=1)
    assert "401" in str(exc.value) and KEY not in str(exc.value)


def test_pexels_rate_limit_is_its_own_error():
    with pytest.raises(StockRateLimited):
        pexels(FakePexelsServer(status=429)).search("ocean", per_page=1)


def test_pexels_download_must_be_an_mp4(tmp_path):
    stock = pexels(FakePexelsServer(body=b"<html>not a video</html>"))
    (video,) = stock.search("ocean", per_page=1)
    with pytest.raises(StockError):
        stock.download(pick_file(video), tmp_path / "a.mp4")
    assert not list(tmp_path.iterdir())  # nothing half-written is left


def test_pexels_refuses_a_file_link_that_is_not_https(tmp_path):
    bad = StockFile(id="9", width=1080, height=1920, link="http://example.com/a.mp4",
                    file_type="video/mp4", quality="hd", fps=25.0)  # fmt: skip
    with pytest.raises(StockError):
        pexels(FakePexelsServer()).download(bad, tmp_path / "a.mp4")


# Picking the file ---------------------------------------------------------------------------


def _video(*sizes: tuple[int, int], file_type: str = "video/mp4") -> StockVideo:
    files = tuple(StockFile(id=str(n), width=w, height=h, link=f"https://v/{n}.mp4",
                            file_type=file_type, quality="hd", fps=25.0)
                  for n, (w, h) in enumerate(sizes))  # fmt: skip
    return StockVideo(id="1", page_url="https://p", duration=10, width=1080, height=1920,
                      creator="A", creator_url="https://c", files=files)  # fmt: skip


def test_pick_file_prefers_the_narrowest_full_hd_portrait():
    assert pick_file(_video((2160, 3840), (1440, 2560), (1080, 1920))).width == 1080


def test_pick_file_never_upscales_or_takes_landscape():
    assert pick_file(_video((720, 1280), (540, 960))) is None
    assert pick_file(_video((1920, 1080), (3840, 2160))) is None
    assert pick_file(_video((1080, 1920), file_type="application/x-mpegURL")) is None
