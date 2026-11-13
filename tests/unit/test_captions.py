"""Caption groups from word timings and the HyperFrames captions project (FR-PRD-6)."""

from pathlib import Path

import pytest

from engine.library.transcribe import Word
from engine.produce.captions import (
    CaptionStyle,
    caption_coverage,
    caption_html,
    clean_words,
    group_words,
    render_captions,
    root_html,
    write_caption_project,
)


def words(*items: tuple[str, float, float]) -> list[Word]:
    return [Word(text, start, end) for text, start, end in items]


SPEECH = words(
    ("Discipline", 0.10, 0.50),
    ("is", 0.56, 0.66),
    ("a", 0.70, 0.74),
    ("skill", 0.76, 1.05),
    ("you", 1.09, 1.20),
    ("can", 1.22, 1.36),
    ("practise.", 1.39, 1.90),
    ("Start", 2.89, 3.20),
    ("small,", 3.27, 3.70),
)


def texts(groups) -> list[list[str]]:
    return [[w.text for w in g.words] for g in groups]


def test_groups_respect_max_words():
    assert texts(group_words(SPEECH[:6]))[0] == ["Discipline", "is", "a", "skill"]


def test_groups_break_at_sentence_end():
    groups = group_words(words(("One", 0.1, 0.3), ("two.", 0.31, 0.5), ("Three", 0.51, 0.7)))
    assert texts(groups) == [["One", "two."], ["Three"]]


def test_groups_break_at_pauses():
    groups = group_words(words(("hold", 0.1, 0.3), ("on", 0.46, 0.6), ("now", 0.61, 0.8)))
    assert texts(groups) == [["hold"], ["on", "now"]]  # 160 ms gap >= 150 ms


def test_groups_never_overlap():
    overrun = words(("a", 0.1, 0.9), ("b.", 0.2, 0.3), ("c", 0.5, 0.8))  # Whisper overlaps
    groups = group_words(overrun, max_words=1)
    for first, second in zip(groups, groups[1:], strict=False):
        assert first.end <= second.start
        assert first.start < first.end


def test_first_group_starts_no_earlier_than_0_1():
    (group,) = group_words(words(("Go", 0.0, 0.4)))
    assert group.start == 0.1 and group.end == 0.4


def test_junk_tokens_dropped():
    cleaned = clean_words(words(("♪", 0, 1), ("...", 1, 2), ("ok", 2, 3), ("", 3, 4)))
    assert [w.text for w in cleaned] == ["ok"]


def test_coverage_counts_speech_inside_groups():
    groups = group_words(SPEECH)
    assert caption_coverage(SPEECH, groups) == pytest.approx(1.0)
    half = [g for g in groups if g.start < 1.0]
    assert 0 < caption_coverage(SPEECH, half) < 0.9


def test_scene_has_one_clip_per_group_and_registers_timeline():
    groups = group_words(SPEECH)
    scene = caption_html(groups, CaptionStyle())
    assert scene.lstrip().startswith("<template>") and scene.rstrip().endswith("</template>")
    assert scene.count('class="cap clip"') == len(groups) == 3
    assert 'data-composition-id="captions"' in scene
    assert 'id="cap-0" class="cap clip" data-start="0.1" data-duration="0.95"' in scene
    assert 'window.__timelines["captions"] = tl;' in scene
    assert "bottom: 650px" in scene


def test_root_hosts_a_captions_track():
    root = root_html(4.5)
    assert '<script src="assets/gsap.min.js"></script>' in root
    assert 'data-composition-src="compositions/captions.html"' in root
    assert 'data-track-kind="captions"' in root
    assert root.count('data-duration="4.5"') == 2  # the root and the captions host
    assert "cdn." not in root


def test_words_are_html_escaped():
    scene = caption_html(group_words(words(('<b>"hi"</b>', 0.2, 0.6))), CaptionStyle())
    assert "&lt;b&gt;&quot;hi&quot;&lt;/b&gt;" in scene
    assert "<b>" not in scene


def test_project_copies_vendored_gsap(tmp_path):
    project = write_caption_project(group_words(SPEECH), 4.0, tmp_path / "captions")
    assert (project / "assets" / "gsap.min.js").read_bytes().startswith(b"/*!\n * GSAP 3.15.0")
    assert (project / "index.html").is_file()
    assert (project / "compositions" / "captions.html").is_file()
    assert (project / "hyperframes.json").is_file()


def test_render_captions_hands_the_project_to_the_renderer(tmp_path):
    seen: list[tuple[Path, Path]] = []

    class FakeRenderer:
        def render(self, composition_dir: Path, output: Path) -> Path:
            seen.append((composition_dir, output))
            return output

    out = render_captions(SPEECH, 4.0, tmp_path / "proj", tmp_path / "captions.mov", FakeRenderer())
    assert out == tmp_path / "captions.mov"
    assert seen == [(tmp_path / "proj", tmp_path / "captions.mov")]
