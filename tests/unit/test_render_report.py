"""FR-PRD-7: render_report.json, built from the plan, its assets' rights, the probed MP4,
its loudness and the caption words. Pure functions; the golden dry run writes a real one."""

import pytest

from engine.library.transcribe import Word
from engine.produce.editplan import EditPlan
from engine.produce.ffmpeg import MediaInfo
from engine.produce.report import (
    AssetFacts,
    build_render_report,
    measure_text_box,
    parse_bbox,
    parse_ebur128,
)

INFO = MediaInfo(
    duration=20.0, width=1080, height=1920, fps=30.0, video_codec="h264", pix_fmt="yuv420p",
    audio_codec="aac", sample_rate=48000, tags={"major_brand": "isom"},
)  # fmt: skip
PLAN = EditPlan.model_validate(
    {
        "timeline": [
            {"asset_id": 1, "in": 0, "out": 10, "role": "avatar"},
            {"asset_id": 2, "in": 0, "out": 4, "role": "evidence"},
            {"asset_id": 3, "in": 0, "out": 6, "role": "broll"},
        ],
        "audio": {"voice": [{"asset_id": 4, "at": 0, "out": 20}]},
        "captions": {"block": "captions"},
        "look": {"id": "selfhelp", "version": 1},
    }
)
FACTS = {
    1: AssetFacts("ai_generated", "none", "video"),
    2: AssetFacts("film_reference", "yes", "video"),
    3: AssetFacts("own", "unknown", "video"),
    4: AssetFacts("ai_generated", "none", "audio"),
}
WORDS = [Word("Start", 0.1, 0.5), Word("small.", 0.6, 1.0), Word("Keep", 5.0, 5.4)]


def report(**overrides):
    args = {
        "plan": PLAN, "facts": FACTS, "info": INFO, "loudness": (-14.1, -1.2),
        "words": WORDS, "targets": (-14.0, -1.0),
    }  # fmt: skip
    return build_render_report(**{**args, **overrides})


def test_report_has_durations_per_tier_and_film_ratios():
    r = report()
    assert r["tiers"] == {"ai_generated": 10.0, "film_reference": 4.0, "own": 6.0}
    assert r["tier_shares"]["film_reference"] == pytest.approx(0.2)
    assert r["film"] == {"seconds": 4.0, "share": 0.2, "longest_excerpt_s": 4.0,
                         "limit_share": 0.3, "limit_excerpt_s": 10.0}  # fmt: skip
    assert r["ai_generated_on_screen"] is True


def test_report_flags_people_by_release_for_pictures_only():
    assert report()["people"] == {"none": [1], "yes": [2], "unknown": [3]}


def test_report_has_loudness_resolution_and_caption_coverage():
    r = report()
    assert r["loudness"] == {"integrated_lufs": -14.1, "true_peak_dbtp": -1.2,
                             "target_lufs": -14.0, "target_true_peak_dbtp": -1.0}  # fmt: skip
    assert r["video"] == {"codec": "h264", "width": 1080, "height": 1920, "fps": 30.0,
                          "pix_fmt": "yuv420p"}  # fmt: skip
    assert r["audio"] == {"codec": "aac", "sample_rate": 48000}
    assert r["duration"] == 20.0 and r["plan_duration"] == 20.0
    assert r["captions"]["words"] == 3 and r["captions"]["coverage"] == pytest.approx(1.0)
    assert r["metadata_tags"] == ["major_brand"]


def test_report_without_captions_or_sound():
    r = report(words=[], loudness=(None, None))
    assert r["captions"] == {"words": 0, "groups": 0, "coverage": None, "box": None}
    assert r["loudness"]["integrated_lufs"] is None


SUMMARY = """[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         {i} LUFS
    Threshold: -24.1 LUFS

  True peak:
    Peak:       {p} dBFS
"""


def test_parse_ebur128_reads_the_summary_and_treats_silence_as_none():
    assert parse_ebur128(SUMMARY.format(i="-14.0", p="-1.3")) == (-14.0, -1.3)
    assert parse_ebur128(SUMMARY.format(i="-70.0", p="-inf")) == (None, None)


def test_report_carries_the_measured_caption_box():
    assert report(caption_box=[150, 1183, 930, 1275])["captions"]["box"] == [150, 1183, 930, 1275]


BBOX = (
    "[Parsed_bbox_2 @ 0x1] n:0 pts:0 pts_time:0 x1:150 x2:900 y1:1190 y2:1270 w:751 h:81\n"
    "[Parsed_bbox_2 @ 0x1] n:1 pts:1 pts_time:0.5 x1:170 x2:930 y1:1183 y2:1275 w:761 h:93\n"
)


def test_parse_bbox_unions_the_frames_and_returns_none_when_nothing_shows():
    assert parse_bbox(BBOX) == [150, 1183, 930, 1275]
    assert parse_bbox("frame=  10 fps=0.0 q=-0.0 size=N/A") is None


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_measure_text_box_reads_a_real_alpha_overlay(tmp_path):
    import subprocess

    overlay = tmp_path / "captions.mov"
    source = (
        "color=c=black@0.0:s=216x384:d=1:r=4,format=yuva444p,"
        "drawbox=x=20:y=240:w=160:h=16:color=white@1.0:t=fill:replace=1"
    )
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", source, "-c:v", "prores_ks",
         "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(overlay)],
        check=True,
    )  # fmt: skip
    assert measure_text_box(overlay) == [20, 240, 179, 255]
