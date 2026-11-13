"""VoiceProvider contract (FR-PRD-2, NFR-8): ElevenLabs on a mocked transport and FakeVoice
pass the same tests; ElevenLabs-only tests pin the request shape and error handling."""

import io
import json
import wave

import httpx
import pytest

from engine.produce.voice import (
    Balance,
    ElevenLabsVoice,
    FakeVoice,
    PronunciationDictionary,
    VoiceError,
    VoiceProvider,
    VoiceRequest,
    silent_wav_bytes,
)

KEY = "test-" + "k" * 20
REQUEST = VoiceRequest(
    script_id=7,
    text="Discipline is a skill you can practise.",
    voice_id="voice-abbas",
    dictionaries=(PronunciationDictionary("dict-1", "v3"),),
)


class FakeElevenLabsServer:
    """Answers like api.elevenlabs.io for the two endpoints the adapter uses."""

    def __init__(self, used=10_000, limit=131_000, status=200, body=None):
        self.used, self.limit, self.status, self.body = used, limit, status, body
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/user/subscription":
            return httpx.Response(
                200,
                json={
                    "character_count": self.used,
                    "character_limit": self.limit,
                    "next_character_count_reset_unix": 1767312000,
                    "tier": "creator",
                },
            )
        if request.url.path.startswith("/v1/text-to-speech/"):
            if self.status != 200:
                return httpx.Response(self.status, text='{"detail":"invalid_api_key"}')
            self.used += len(json.loads(request.content)["text"])
            return httpx.Response(200, content=self.body or silent_wav_bytes(1.0))
        return httpx.Response(404)


def eleven(server: FakeElevenLabsServer) -> ElevenLabsVoice:
    return ElevenLabsVoice(api_key=KEY, transport=httpx.MockTransport(server))


@pytest.fixture(params=["elevenlabs", "fake"])
def provider(request) -> VoiceProvider:
    if request.param == "elevenlabs":
        return eleven(FakeElevenLabsServer(used=10_000, limit=131_000))
    return FakeVoice(used=10_000, limit=131_000)


def test_is_a_voice_provider(provider):
    assert isinstance(provider, VoiceProvider)


def test_balance_reports_remaining(provider):
    balance = provider.balance()
    assert isinstance(balance, Balance)
    assert (balance.used, balance.limit, balance.remaining) == (10_000, 131_000, 121_000)


def test_synthesize_writes_a_48k_wav(provider, tmp_path):
    out = provider.synthesize(REQUEST, tmp_path / "voice" / "script-7.wav")
    assert out == tmp_path / "voice" / "script-7.wav"
    with wave.open(str(out)) as wav:
        assert wav.getframerate() == 48000
        assert wav.getnframes() > 0


def test_balance_drops_by_the_characters_sent(provider, tmp_path):
    provider.synthesize(REQUEST, tmp_path / "a.wav")
    assert provider.balance().used == 10_000 + len(REQUEST.text)


def test_request_carries_key_model_format_and_dictionaries(tmp_path):
    server = FakeElevenLabsServer()
    eleven(server).synthesize(REQUEST, tmp_path / "a.wav")
    (call,) = server.requests
    assert call.headers["xi-api-key"] == KEY
    assert call.url.path == "/v1/text-to-speech/voice-abbas"
    assert call.url.params["output_format"] == "wav_48000"
    body = json.loads(call.content)
    assert body["model_id"] == "eleven_multilingual_v2"
    assert body["pronunciation_dictionary_locators"] == [
        {"pronunciation_dictionary_id": "dict-1", "version_id": "v3"}
    ]


def test_api_error_is_reported_without_the_key(tmp_path):
    with pytest.raises(VoiceError, match="401") as exc:
        eleven(FakeElevenLabsServer(status=401)).synthesize(REQUEST, tmp_path / "a.wav")
    assert KEY not in str(exc.value)
    assert not (tmp_path / "a.wav").exists()


def test_a_body_that_is_not_wav_is_refused(tmp_path):
    with pytest.raises(VoiceError, match="WAV"):
        eleven(FakeElevenLabsServer(body=b"ID3 not a wav")).synthesize(REQUEST, tmp_path / "a.wav")


def test_silent_wav_bytes_are_48k_mono():
    with wave.open(io.BytesIO(silent_wav_bytes(0.5))) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getnframes()) == (48000, 1, 24000)
