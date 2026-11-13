"""ffmpeg compositing and probing (SRD §7.2, FR-PRD-5, NFR-8). Fake runners only."""

import json
import subprocess
from pathlib import Path

import pytest

from engine.produce.ffmpeg import FfmpegError, composite_command, composite_overlay, probe

FFPROBE_JSON = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920,
         "r_frame_rate": "30/1", "pix_fmt": "yuv420p", "tags": {"handler_name": "VideoHandler"}},
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000",
         "r_frame_rate": "0/0", "tags": {"language": "und"}},
    ],
    "format": {"duration": "5.013000", "tags": {"major_brand": "isom"}},
}  # fmt: skip


def fake_run(returncode=0, stdout="", stderr=""):
    calls = []

    def run(argv, extra_env=None):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    run.calls = calls
    return run


def test_probe_parses_ffprobe_json(tmp_path):
    info = probe(tmp_path / "a.mp4", run=fake_run(stdout=json.dumps(FFPROBE_JSON)))
    assert (info.width, info.height, info.fps) == (1080, 1920, 30.0)
    assert (info.video_codec, info.pix_fmt) == ("h264", "yuv420p")
    assert (info.audio_codec, info.sample_rate) == ("aac", 48000)
    assert info.duration == pytest.approx(5.013)
    assert info.tags == {"major_brand": "isom", "handler_name": "VideoHandler", "language": "und"}


def test_probe_without_audio(tmp_path):
    video_only = {**FFPROBE_JSON, "streams": FFPROBE_JSON["streams"][:1]}
    info = probe(tmp_path / "a.mov", run=fake_run(stdout=json.dumps(video_only)))
    assert info.audio_codec is None and info.sample_rate is None


def test_probe_failure_raises(tmp_path):
    with pytest.raises(FfmpegError, match="No such file"):
        probe(tmp_path / "missing.mp4", run=fake_run(1, stderr="missing.mp4: No such file"))


def test_composite_command_meets_fr_prd_5():
    cmd = composite_command(Path("base.mp4"), Path("overlay.mov"), Path("out.mp4"))
    joined = " ".join(cmd)
    assert cmd[0] == "ffmpeg"
    assert "overlay=0:0:format=auto,format=yuv420p" in joined
    assert cmd[cmd.index("-c:v") + 1] == "h264_nvenc"
    assert cmd[cmd.index("-r") + 1] == "30"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert cmd[cmd.index("-ar") + 1] == "48000"
    assert cmd[cmd.index("-map_metadata") + 1] == "-1"
    assert cmd[cmd.index("-metadata:s:v") + 1] == "encoder="
    assert cmd[cmd.index("-fflags") + 1] == "+bitexact"
    assert cmd[-1] == "out.mp4"


def test_composite_command_maps_optional_audio():
    cmd = composite_command(Path("base.mp4"), Path("overlay.mov"), Path("out.mp4"))
    maps = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"]
    assert maps == ["[v]", "0:a?"]


def test_composite_encoder_can_be_swapped():
    cmd = composite_command(Path("b"), Path("o"), Path("x"), encoder="libx264")
    assert cmd[cmd.index("-c:v") + 1] == "libx264"


def test_composite_failure_raises(tmp_path):
    run = fake_run(1, stderr="Error initializing output stream: No NVENC capable devices")
    with pytest.raises(FfmpegError, match="NVENC"):
        composite_overlay(tmp_path / "b.mp4", tmp_path / "o.mov", tmp_path / "x.mp4", run=run)


def test_composite_returns_output(tmp_path):
    run = fake_run()
    out = composite_overlay(tmp_path / "b.mp4", tmp_path / "o.mov", tmp_path / "x.mp4", run=run)
    assert out == tmp_path / "x.mp4"
    assert run.calls[0][-1] == str(tmp_path / "x.mp4")
