"""A real Pexels search (FR-ING-5): one request, no download. Pexels is free, but this
reaches a real account, so it carries the `live` marker and runs only with `-m live` once
`PEXELS_API_KEY` is in `.env` (G0.9a, P0.6b.3)."""

import pytest

from engine.library.stock import PexelsStock, pick_file
from engine.settings import load_settings

pytestmark = pytest.mark.live


def test_a_real_pexels_search_returns_portrait_videos_with_credits():
    key = load_settings().get("PEXELS_API_KEY")
    if key is None:
        pytest.skip("PEXELS_API_KEY is missing (G0.9a)")
    videos = PexelsStock(api_key=key).search("ocean waves", per_page=3)
    assert videos, "Pexels found nothing for 'ocean waves'"
    for video in videos:
        assert video.page_url.startswith("https://www.pexels.com/")
        assert video.creator and video.creator_url
    assert any(pick_file(v) for v in videos)
