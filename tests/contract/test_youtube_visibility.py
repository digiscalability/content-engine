"""YouTube visibility (FR-PUB-3, ADR 0021): a private-route post becomes `published` once
YouTube's key-free oEmbed endpoint answers for it. Only a 200 counts as public, so anything
unexpected keeps the post waiting for Abbas instead of marking it live."""

import httpx
import pytest

from engine.publish.youtube import (
    FakeVisibility,
    OEmbedVisibility,
    Visibility,
    VisibilityError,
)

VIDEO = "dQw4w9WgXcQ"


def oembed(status: int, seen: list | None = None) -> OEmbedVisibility:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json={"title": "t"} if status == 200 else None)

    return OEmbedVisibility(transport=httpx.MockTransport(handler))


def test_both_are_visibility():
    assert isinstance(OEmbedVisibility(), Visibility)
    assert isinstance(FakeVisibility(), Visibility)


def test_a_200_is_public_and_the_request_carries_no_key():
    seen = []
    assert oembed(200, seen).is_public(VIDEO) is True
    (request,) = seen
    assert request.url.host == "www.youtube.com" and request.url.path == "/oembed"
    assert request.url.params["url"] == f"https://www.youtube.com/watch?v={VIDEO}"
    assert request.url.params["format"] == "json"
    assert "authorization" not in request.headers and "key" not in request.url.params


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_refusals_mean_not_public(status):
    assert oembed(status).is_public(VIDEO) is False


@pytest.mark.parametrize("status", [429, 500, 503])
def test_other_answers_are_errors(status):
    with pytest.raises(VisibilityError, match=str(status)):
        oembed(status).is_public(VIDEO)


def test_a_transport_failure_is_an_error():
    def boom(request):
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(VisibilityError, match="offline"):
        OEmbedVisibility(transport=httpx.MockTransport(boom)).is_public(VIDEO)


@pytest.mark.parametrize("bad", ["", "short", "dQw4w9WgXcQ&x=1", "../../etc/pass", "missing"])
def test_only_youtube_shaped_ids_are_asked(bad):
    seen = []
    with pytest.raises(VisibilityError, match="video id"):
        oembed(200, seen).is_public(bad)
    assert seen == []


def test_the_fake_knows_which_videos_are_public():
    fake = FakeVisibility(public={VIDEO})
    assert fake.is_public(VIDEO) is True
    assert fake.is_public("aaaaaaaaaaa") is False


@pytest.mark.live
def test_live_a_long_public_video_is_public():
    assert OEmbedVisibility().is_public("jNQXAC9IVRw") is True  # "Me at the zoo", 2005
