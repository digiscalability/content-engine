"""P0.4 done-when (SRD §7.5): a fixed format C edit plan rendered from generated fixtures,
checked for duration, resolution, loudness and frame hashes within tolerance.

Run with `uv run pytest -m golden`. After a deliberate change to the renderer, look at the
frames, then refresh the stored hashes with `UPDATE_GOLDEN=1 uv run pytest -m golden`.
"""

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from engine.produce.editplan import EditPlan
from engine.produce.ffmpeg import probe
from engine.produce.render_ffmpeg import RenderSettings, RenderSources, render

from . import media

pytestmark = pytest.mark.golden

EXPECTED = Path(__file__).parent / "expected" / "format_c_thumbs.json"
THUMB_TIMES = [0.5, 1.5, 2.5, 3.2, 4.0, 5.5]
OVERLAY_BOX = (140, 1200, 800, 200)

PLAN = {
    "timeline": [
        {"asset_id": 1, "in": 0.0, "out": 2.0, "role": "avatar",
         "ops": {"zoom": {"from_scale": 1.0, "to_scale": 1.3, "ease": "in_out"}}},
        {"asset_id": 2, "in": 1.0, "out": 3.0, "role": "broll", "ops": {"speed": 2.0}},
        {"asset_id": 2, "in": 3.0, "out": 4.0, "role": "broll",
         "ops": {"freeze": {"at": 0.5, "duration": 1.0}}},
        {"asset_id": 3, "in": 0.0, "out": 1.0, "role": "card"},
    ],
    "audio": {
        "voice": [{"asset_id": 10, "at": 1.5, "out": 3.0}],
        "music": {"asset_id": 11, "gain_db": -6, "duck_db": 12},
        "sfx": [{"asset_id": 12, "at": 5.0, "gain_db": -6}],
    },
    "overlays": [{"block": "lower-third", "kind": "lower_third", "start": 0.5, "duration": 2.0}],
    "look": {"id": "golden", "version": 1},
}  # fmt: skip


def nvenc_available() -> bool:
    encoders = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
    )
    return "h264_nvenc" in encoders.stdout


def render_golden_plan(d: Path) -> Path:
    """Generate the fixtures in `d` and render PLAN from them; returns the MP4."""
    sources = RenderSources(
        assets={
            1: media.video(d / "avatar.mp4", "testsrc2=size=1080x1920:rate=30:duration=3"),
            2: media.video(d / "broll 16x9.mp4", "testsrc=size=1920x1080:rate=30:duration=5"),
            3: media.video(d / "grey.mp4", "color=c=0x808080:size=1080x1920:rate=30:duration=2"),
            10: media.tone(d / "vo.wav", 1000, 3),
            11: media.tone(d / "music.wav", 220, 4),  # shorter than the plan: must loop
            12: media.tone(d / "sfx.wav", 3000, 0.2),
        },
        overlays={"lower-third": media.alpha_box(d / "lt.mov", 2.0, OVERLAY_BOX, "yellow")},
        lut=media.warm_lut(d / "warm look.cube"),
    )
    encoder = "h264_nvenc" if nvenc_available() else "libx264"
    plan = EditPlan.model_validate(PLAN)
    result = render(plan, sources, d / "out.mp4", d / "work", RenderSettings(encoder=encoder))
    return result.path


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> Path:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not on PATH")
    return render_golden_plan(tmp_path_factory.mktemp("format c"))  # a space, like real folders


def test_duration_and_fr_prd_5_spec(rendered):
    info = probe(rendered)
    assert info.duration == pytest.approx(6.0, abs=0.05)
    assert (info.video_codec, info.width, info.height, info.fps) == ("h264", 1080, 1920, 30.0)
    assert info.pix_fmt == "yuv420p"
    assert (info.audio_codec, info.sample_rate) == ("aac", 48000)
    assert {"encoder", "creation_time", "location"}.isdisjoint(info.tags), info.tags
    frames = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=nb_frames",
         "-of", "csv=p=0", str(rendered)], capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert abs(int(frames) - 180) <= 1


def test_loudness_meets_target(rendered):
    integrated, true_peak = media.loudness(rendered)
    assert integrated == pytest.approx(-14.0, abs=1.0)
    assert true_peak <= -0.5


def test_music_ducks_under_voice(rendered):
    before = media.band_rms_db(rendered, 0.2, 1.0, 220)
    under = media.band_rms_db(rendered, 2.5, 1.5, 220)
    assert before - under == pytest.approx(12.0, abs=2.5)


def test_freeze_holds_one_frame(rendered):
    held = media.mean_abs_diff(media.thumbnail(rendered, 3.6), media.thumbnail(rendered, 4.4))
    moving = media.mean_abs_diff(
        media.raw_frame(rendered, 3.05, "scale=270:480", "gray"),
        media.raw_frame(rendered, 3.45, "scale=270:480", "gray"),
    )
    assert held < 1.0
    assert moving > 1.0


def test_lut_warms_the_grey_shot(rendered):
    red, _, blue = media.rgb_means(rendered, 5.5, (0, 0, 1080, 1920))
    assert red - blue > 20


def test_overlay_is_composited_after_the_grade(rendered):
    red, green, blue = media.rgb_means(rendered, 1.0, OVERLAY_BOX)
    assert red > 200 and green > 200 and blue < 60  # pure yellow, not tinted by the LUT


def test_overlay_leaves_the_picture_visible_around_it(rendered):
    luma = media.raw_frame(rendered, 1.0, "crop=1080:300:0:100,format=gray", "gray")
    assert sum(luma) / len(luma) > 60  # the avatar pattern, not a black transparent area


def test_frames_match_golden_hashes(rendered):
    thumbs = {str(t): media.thumbnail(rendered, t) for t in THUMB_TIMES}
    if os.environ.get("UPDATE_GOLDEN") == "1":
        EXPECTED.parent.mkdir(exist_ok=True)
        encoded = {t: base64.b64encode(b).decode("ascii") for t, b in thumbs.items()}
        EXPECTED.write_text(json.dumps(encoded, indent=1) + "\n", encoding="utf-8")
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    for t, thumb in thumbs.items():
        diff = media.mean_abs_diff(thumb, base64.b64decode(expected[t]))
        assert diff <= 8, f"frame at {t}s differs from golden by {diff:.1f}"
