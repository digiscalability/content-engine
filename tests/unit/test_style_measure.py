"""Style measurements (SRD §9.1) as pure functions over ffmpeg output and pixel arrays."""

import numpy as np
import pytest

from engine.style.measure import (
    TextBox,
    brightness_saturation,
    palette,
    parse_scene_cuts,
    shot_stats,
    tempo_bpm,
    text_stats,
)

SHOWINFO = """[Parsed_showinfo_1 @ 0x1] n:   0 pts:  60 pts_time:2.0     duration:1
[Parsed_showinfo_1 @ 0x1] n:   1 pts: 120 pts_time:4.0     duration:1
[Parsed_showinfo_1 @ 0x1] n:   2 pts: 165 pts_time:5.5     duration:1
"""


def test_scene_cuts_come_from_showinfo_times():
    assert parse_scene_cuts(SHOWINFO) == [2.0, 4.0, 5.5]
    assert parse_scene_cuts("no frames selected") == []


def test_shot_stats_measure_the_shots_between_cuts():
    stats = shot_stats([2.0, 4.0], duration=6.0)
    assert stats == {"shots": 3, "cuts_per_min": 20.0, "shot_median_s": 2.0,
                     "shot_p10_s": 2.0, "shot_p90_s": 2.0}  # fmt: skip
    assert shot_stats([], duration=10.0)["shots"] == 1


def test_palette_finds_the_dominant_colours_in_order():
    red = np.tile([250, 10, 10], (600, 1))
    blue = np.tile([10, 10, 240], (400, 1))
    swatches = palette(np.vstack([red, blue]).astype(np.uint8), k=5)
    assert swatches[0] == "#fa0a0a" and swatches[1] == "#0a0af0"
    assert len(swatches) <= 5


def test_brightness_and_saturation():
    assert brightness_saturation(np.array([[255, 0, 0]], np.uint8)) == pytest.approx((1.0, 1.0))
    v, s = brightness_saturation(np.array([[128, 128, 128]], np.uint8))
    assert v == pytest.approx(128 / 255, abs=1e-4) and s == 0.0  # stored to 4 places


def clicks(bpm: float, seconds: float, rate: int = 11025) -> np.ndarray:
    samples = np.zeros(int(seconds * rate), dtype=np.float32)
    for t in np.arange(0, seconds, 60 / bpm):
        start = int(t * rate)
        samples[start : start + 200] = 0.8  # a short click
    return samples


@pytest.mark.parametrize("bpm", [90, 120, 150])
def test_tempo_from_an_onset_envelope(bpm):
    assert tempo_bpm(clicks(bpm, 12.0), 11025) == pytest.approx(bpm, abs=3)


def test_tempo_of_silence_is_none():
    assert tempo_bpm(np.zeros(11025 * 5, dtype=np.float32), 11025) is None


def test_text_stats_from_detected_boxes():
    caption = TextBox(x=200, y=1300, w=680, h=90, text="start small today")
    title = TextBox(x=100, y=200, w=880, h=120, text="FOCUS")
    frames = [(0.0, []), (1.0, [caption]), (2.0, [caption, title]), (3.0, [])]
    stats = text_stats(frames, width=1080, height=1920)
    assert stats["text_frame_share"] == 0.5 and stats["first_text_s"] == 1.0
    assert stats["caption_y"] == pytest.approx((1300 + 45) / 1920, abs=1e-3)
    assert stats["caption_height"] == pytest.approx(90 / 1920, abs=1e-3)
    assert stats["words_per_page"] == 3


def test_text_stats_without_text():
    stats = text_stats([(0.0, []), (1.0, [])], width=1080, height=1920)
    assert stats == {"text_frame_share": 0.0, "first_text_s": None, "caption_y": None,
                     "caption_height": None, "words_per_page": None}  # fmt: skip


def test_loudness_and_range_from_an_ebur128_summary():
    from engine.style.measure import parse_loudness_range

    summary = """[Parsed_ebur128_0 @ 0x1] Summary:

  Integrated loudness:
    I:         -15.2 LUFS
    Threshold: -25.3 LUFS

  Loudness range:
    LRA:         7.4 LU
"""
    assert parse_loudness_range(summary) == (-15.2, 7.4)
    assert parse_loudness_range(summary.replace("-15.2", "-70.0")) == (None, None)  # silence


def test_words_are_estimated_when_ocr_drops_the_spaces():
    from engine.style.measure import word_count

    assert word_count("START SMALL TODAY") == 3
    assert word_count("STARTSMALLTODAY") == 3  # about five letters a word
    assert word_count("GO") == 1
    assert word_count("Understanding discipline") == 2  # spaces present: counted, not guessed
