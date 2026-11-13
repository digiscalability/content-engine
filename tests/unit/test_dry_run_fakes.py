"""Fakes the dry run needs beyond P0.6's: an avatar that renders a card video at once, and a
transcriber that lays the script's words over the (silent) fake voice."""

import struct
import subprocess

from engine.library.transcribe import ScriptAlignedTranscriber
from engine.produce.avatar import AvatarRequest, FakeAvatar
from engine.produce.ffmpeg import wav_duration
from engine.produce.voice import silent_wav_bytes


def extensible_wav(seconds: float, rate: int = 48000, channels: int = 2) -> bytes:
    """A 24-bit WAVE_FORMAT_EXTENSIBLE file with a LIST chunk before the data, as ffmpeg
    writes for pcm_s24le."""
    block = channels * 3
    fmt = struct.pack("<HHIIHH", 0xFFFE, channels, rate, rate * block, block, 24)
    fmt += struct.pack("<HHI16s", 22, 24, 3, b"\x01\x00\x00\x00\x00\x00\x10\x00" + b"\x00" * 8)
    data = b"\x00" * round(seconds * rate) * block
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"LIST" + struct.pack("<I", 4) + b"INFO"
    chunks += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def test_wav_duration_reads_plain_and_extensible_headers(tmp_path):
    plain = tmp_path / "plain.wav"
    plain.write_bytes(silent_wav_bytes(2.5))
    extensible = tmp_path / "mix.wav"
    extensible.write_bytes(extensible_wav(1.25))
    assert wav_duration(plain) == 2.5
    assert wav_duration(extensible) == 1.25


def test_script_aligned_words_cover_the_audio_in_order(tmp_path):
    wav = tmp_path / "mix.wav"
    wav.write_bytes(silent_wav_bytes(4.0))
    text = "Start smaller than you think. Then keep going."
    words = ScriptAlignedTranscriber(text).transcribe(wav).words
    assert [w.text for w in words] == text.split()
    assert words[0].start == 0.1 and words[-1].end <= 4.0
    assert all(a.end <= b.start for a, b in zip(words, words[1:], strict=False))
    assert all(w.end > w.start for w in words)


class Recorder:
    def __init__(self):
        self.argv: list[list[str]] = []

    def __call__(self, argv, env=None):
        self.argv.append(argv)
        output = argv[-1]
        with open(output, "wb") as handle:
            handle.write(b"mp4")
        return subprocess.CompletedProcess(argv, 0, "", "")


def request(tmp_path) -> AvatarRequest:
    audio = tmp_path / "script-3.wav"
    audio.write_bytes(silent_wav_bytes(6.0))
    return AvatarRequest(brief_id=7, audio=audio, avatar_id="dry-run", script="Hello there.")


def test_fake_avatar_renders_a_card_of_the_voice_length_at_once(tmp_path):
    run = Recorder()
    avatar = FakeAvatar(inbox=tmp_path / "inbox", run=run)
    status = avatar.request(request(tmp_path))
    assert status.state == "ready" and status.video == tmp_path / "inbox" / "7.mp4"
    (argv,) = run.argv
    assert "d=6" in " ".join(argv) and "1080x1920" in " ".join(argv)
    avatar.request(request(tmp_path))  # an existing video is reused, never re-rendered
    assert len(run.argv) == 1


def test_fake_avatar_can_leave_a_request_waiting(tmp_path):
    avatar = FakeAvatar(inbox=tmp_path / "inbox", ready=False, run=Recorder())
    status = avatar.request(request(tmp_path))
    assert status.state == "waiting" and status.video is None and "brief 7" in status.detail
