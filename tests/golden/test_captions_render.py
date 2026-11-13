"""P0.5 done-when, the caption fixture test (FR-PRD-6): real large-v3 word timings for the
generated speech fixture become caption groups, a HyperFrames captions composition renders
them to an alpha overlay, and the composite shows each word's group while it is spoken.

Run with `uv run pytest -m golden`. The word timings are recorded by
`scripts/make_whisper_fixture.py`, so this test needs no GPU.
"""

import json
import shutil
from pathlib import Path

import pytest

from engine.library.transcribe import Word
from engine.produce.captions import caption_coverage, group_words, render_captions
from engine.produce.ffmpeg import composite_overlay, probe
from engine.produce.overlay import HyperFramesRenderer

from . import media

pytestmark = pytest.mark.golden

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "speech" / "discipline.whisper.json"
WORDS = [
    Word(w["text"], w["start"], w["end"], w["probability"])
    for w in json.loads(FIXTURE.read_text(encoding="utf-8"))["words"]
]
DURATION = 7.5
CAPTION_BAND = (60, 1150, 960, 130)  # around the caption line, 650 px from the bottom


@pytest.fixture(scope="module")
def captioned(tmp_path_factory) -> dict[str, Path]:
    for tool in ("npx", "ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not on PATH")
    d = tmp_path_factory.mktemp("captions")
    overlay = render_captions(
        WORDS, DURATION, d / "project", d / "captions.mov", HyperFramesRenderer()
    )
    base = media.video(d / "base.mp4", f"testsrc2=size=1080x1920:rate=30:duration={DURATION}")
    composite = composite_overlay(base, overlay, d / "captioned.mp4", encoder="libx264")
    return {"overlay": overlay, "base": base, "composite": composite}


def band_diff(clips: dict[str, Path], at: float) -> float:
    return media.mean_abs_diff(
        media.raw_frame(clips["base"], at, _crop(), "gray"),
        media.raw_frame(clips["composite"], at, _crop(), "gray"),
    )


def _crop() -> str:
    x, y, w, h = CAPTION_BAND
    return f"crop={w}:{h}:{x}:{y}"


def test_groups_follow_sentences_and_pauses():
    groups = [" ".join(w.text for w in g.words) for g in group_words(WORDS)]
    assert groups == [
        "Discipline is a skill",
        "you can practice.",
        "Start small,",
        "keep the promise,",
        "and repeat it tomorrow.",
    ]
    assert caption_coverage(WORDS, group_words(WORDS)) >= 0.95  # FR-QA-4


def test_caption_overlay_is_alpha_and_full_length(captioned):
    info = probe(captioned["overlay"])
    assert info.pix_fmt.startswith("yuva")
    assert info.duration == pytest.approx(DURATION, abs=0.05)


def test_every_word_shows_its_caption(captioned):
    for word in WORDS:
        middle = (word.start + word.end) / 2
        assert band_diff(captioned, middle) > 15, f"no caption while '{word.text}' is spoken"


def test_no_caption_before_speech_or_after_it(captioned):
    assert band_diff(captioned, 0.03) < 4
    assert band_diff(captioned, 7.2) < 4
