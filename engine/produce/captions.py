"""Captions from word timings (SRD FR-PRD-6): caption groups written as a HyperFrames
captions composition, rendered to an alpha overlay (ADR 0001) and composited after the grade.

Grouping follows the HyperFrames caption rules: 3-5 words a group (default at most 4),
a new group at a sentence end, a pause of 150 ms or more, or the word limit; one group on
screen at a time; nothing before 0.1 s. The style is a stand-in until a look supplies it
(P0.8). Choices are recorded in ADR 0009.
"""

from __future__ import annotations

import html
import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template

from engine.library.transcribe import Word, normalise, speech_onsets
from engine.produce.editplan import EditPlan
from engine.produce.overlay import OverlayRenderer
from engine.produce.process import Runner, run_command
from engine.produce.render_ffmpeg import RenderSources, num
from engine.settings import REPO_ROOT

TEMPLATE = REPO_ROOT / "hyperframes" / "captions"
SCENE = Template((TEMPLATE / "scene.template.html").read_text(encoding="utf-8"))
ROOT = Template((TEMPLATE / "root.template.html").read_text(encoding="utf-8"))
SENTENCE_END = (".", "!", "?")


@dataclass(frozen=True)
class CaptionGroup:
    words: tuple[Word, ...]
    start: float
    end: float


@dataclass(frozen=True)
class CaptionStyle:
    font_family: str = "Inter"
    font_weight: int = 900
    font_size: int = 92  # the largest size; long groups shrink to fit max_width
    max_width: int = 740  # inside every platform's safe zone (ADR 0018)
    bottom: int = 650  # px from the bottom: clear of the platform UI, under the face
    ink: str = "#ffffff"
    accent: str = "#ffd23f"  # the word being spoken
    outline: str = "#111111"
    outline_px: int = 14


def clean_words(words: Sequence[Word]) -> list[Word]:
    """Drop tokens with no letters or digits (Whisper's music notes, stray punctuation)."""
    return [w for w in words if normalise(w.text)]


def group_words(
    words: Sequence[Word], max_words: int = 4, pause: float = 0.15, min_start: float = 0.1
) -> list[CaptionGroup]:
    batches: list[list[Word]] = []
    for word in clean_words(words):
        current = batches[-1] if batches else None
        if (
            current is None
            or len(current) >= max_words
            or word.start - current[-1].end >= pause
            or current[-1].text.rstrip().endswith(SENTENCE_END)
        ):
            batches.append([word])
        else:
            current.append(word)
    groups: list[CaptionGroup] = []
    for i, batch in enumerate(batches):
        start = max(batch[0].start, min_start)
        end = batch[-1].end
        if i + 1 < len(batches):
            end = min(end, batches[i + 1][0].start)  # never two groups on screen
        end = max(end, start + 0.05)
        if groups and start < groups[-1].end:
            start = groups[-1].end
        groups.append(CaptionGroup(tuple(batch), round(start, 3), round(end, 3)))
    return groups


def caption_coverage(words: Sequence[Word], groups: Sequence[CaptionGroup]) -> float:
    """Share of speech time (word spans) during which a caption group is showing (FR-QA-4)."""
    spoken = covered = 0.0
    for word in clean_words(words):
        length = max(word.end - word.start, 0.0)
        spoken += length
        for group in groups:
            covered += max(0.0, min(word.end, group.end) - max(word.start, group.start))
    return covered / spoken if spoken else 1.0


def voice_onsets(plan: EditPlan, sources: RenderSources, run: Runner = run_command) -> list[float]:
    """Where speech starts in the final timeline, from the plan's VO sources (ADR 0009)."""
    onsets: list[float] = []
    for segment in plan.audio.voice:
        onsets.append(segment.at)
        for t in speech_onsets(sources.asset(segment.asset_id), run=run):
            if segment.in_s < t < segment.out_s:
                onsets.append(round(segment.at + t - segment.in_s, 3))
    return sorted(set(onsets))


def _json(value: object) -> str:
    return json.dumps(value).replace("<", "\\u003c")  # safe inside <script>


def caption_html(groups: Sequence[CaptionGroup], style: CaptionStyle) -> str:
    """The captions sub-composition: one timed clip per group, word highlights by GSAP."""
    clips = []
    timing = []
    for i, group in enumerate(groups):
        spans = "".join(
            f'<span id="w-{i}-{k}" class="w">{html.escape(w.text)}</span>'
            for k, w in enumerate(group.words)
        )
        clips.append(
            f'    <div id="cap-{i}" class="cap clip" data-start="{num(group.start)}"'
            f' data-duration="{num(group.end - group.start)}" data-track-index="1">'
            f'<div id="cap-{i}-line" class="line">{spans}</div></div>'
        )
        timing.append(
            {
                "line": f"#cap-{i}-line",
                "text": " ".join(w.text for w in group.words),
                "start": group.start,
                "end": group.end,
                "words": [
                    [f"#w-{i}-{k}", max(w.start, group.start), min(w.end, group.end)]
                    for k, w in enumerate(group.words)
                ],
            }
        )
    values = {f: getattr(style, f) for f in style.__dataclass_fields__}
    return SCENE.substitute(values, clips="\n".join(clips), groups_json=_json(timing))


def root_html(duration: float) -> str:
    """The project root: transparent, hosting the captions composition on a captions track."""
    return ROOT.substitute(duration=num(duration))


def write_caption_project(
    groups: Sequence[CaptionGroup],
    duration: float,
    project_dir: Path,
    style: CaptionStyle = CaptionStyle(),
) -> Path:
    (project_dir / "compositions").mkdir(parents=True, exist_ok=True)
    (project_dir / "assets").mkdir(exist_ok=True)
    shutil.copyfile(TEMPLATE / "assets" / "gsap.min.js", project_dir / "assets" / "gsap.min.js")
    (project_dir / "index.html").write_text(root_html(duration), encoding="utf-8")
    scene = caption_html(groups, style)
    (project_dir / "compositions" / "captions.html").write_text(scene, encoding="utf-8")
    config = {"paths": {"assets": "assets"}, "media": {"autoProxy": True}}
    (project_dir / "hyperframes.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    meta = {"id": project_dir.name, "name": project_dir.name}
    (project_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return project_dir


def render_captions(
    words: Sequence[Word],
    duration: float,
    project_dir: Path,
    output: Path,
    renderer: OverlayRenderer,
    style: CaptionStyle = CaptionStyle(),
    max_words: int = 4,
) -> Path:
    """Group the words, write the project and render it to an alpha overlay at `output`."""
    write_caption_project(group_words(words, max_words=max_words), duration, project_dir, style)
    return renderer.render(project_dir, output)
