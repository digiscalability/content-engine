"""P0.3 done-when: HyperFrames renders the caption spike with alpha on this PC, ffmpeg
composites it over a generated clip, and the MP4 passes an ffprobe check (SRD §7.2, C9).

Run with `uv run pytest -m golden`. Needs Node 22+ (npx), ffmpeg and ffprobe on PATH; the
first run on a machine downloads the pinned HyperFrames CLI and its headless Chrome.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from engine.produce.ffmpeg import composite_overlay, probe
from engine.produce.overlay import HyperFramesRenderer
from engine.settings import REPO_ROOT

pytestmark = pytest.mark.golden

SPIKE = REPO_ROOT / "hyperframes" / "captions-spike"
CAPTION_BOX = (60, 1150, 960, 130)  # x, y, w, h around the caption line (650 px from the bottom)
CLEAR_BOX = (0, 100, 1080, 300)  # a band the overlay never paints


def nvenc_available() -> bool:
    encoders = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
    ).stdout
    return "h264_nvenc" in encoders


def make_base_clip(path: Path) -> Path:
    """A generated 5 s 1080x1920 test pattern with a 440 Hz tone (NFR-9: no real footage)."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=30:duration=5",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=5",
         "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(path)],
        check=True,
    )  # fmt: skip
    return path


def region_luma(path: Path, at: float, box: tuple[int, int, int, int]) -> bytes:
    x, y, w, h = box
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
         "-vf", f"crop={w}:{h}:{x}:{y},format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip


def mean_abs_diff(a: bytes, b: bytes) -> float:
    assert len(a) == len(b) > 0
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


@pytest.fixture(scope="module")
def spike(tmp_path_factory) -> dict[str, Path]:
    for tool in ("npx", "ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not on PATH")
    work = tmp_path_factory.mktemp("spike")
    base = make_base_clip(work / "base.mp4")
    overlay = HyperFramesRenderer().render(SPIKE, work / "overlay.mov")
    encoder = "h264_nvenc" if nvenc_available() else "libx264"
    composite = composite_overlay(base, overlay, work / "composite.mp4", encoder=encoder)
    return {"base": base, "overlay": overlay, "composite": composite}


def test_spike_overlay_has_alpha(spike):
    info = probe(spike["overlay"])
    assert info.video_codec == "prores"
    assert info.pix_fmt.startswith("yuva")
    assert (info.width, info.height) == (1080, 1920)
    assert info.duration == pytest.approx(5.0, abs=0.05)


def test_spike_composite_passes_ffprobe_check(spike):
    info = probe(spike["composite"])
    assert info.video_codec == "h264"
    assert (info.width, info.height) == (1080, 1920)
    assert info.fps == 30.0
    assert info.pix_fmt == "yuv420p"
    assert info.duration == pytest.approx(5.0, abs=0.1)
    assert (info.audio_codec, info.sample_rate) == ("aac", 48000)
    leaked = {"encoder", "creation_time", "location", "com.apple.quicktime.location.ISO6709"}
    assert leaked.isdisjoint(info.tags), info.tags


def test_spike_composite_shows_captions(spike):
    base = region_luma(spike["base"], 1.0, CAPTION_BOX)
    composite = region_luma(spike["composite"], 1.0, CAPTION_BOX)
    assert mean_abs_diff(base, composite) > 20


def test_spike_composite_keeps_background_where_overlay_is_clear(spike):
    base = region_luma(spike["base"], 1.0, CLEAR_BOX)
    composite = region_luma(spike["composite"], 1.0, CLEAR_BOX)
    assert mean_abs_diff(base, composite) < 4
