"""FR-PRD-7: `render_report.json` for every render (ADR 0013).

What the render contains, for QA, the qa-reviewer and Abbas: on-screen seconds per rights
tier, film excerpt ratios (§8.2), people flags by release (§8.3), whether AI-made pictures
are on screen (FR-QA-5), loudness against the target, resolution and codecs as probed, the
tags left after metadata stripping, and caption coverage of the speech (FR-QA-4).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from engine.library.transcribe import Word
from engine.produce.captions import caption_coverage, group_words
from engine.produce.editplan import FILM_EXCERPT_MAX_S, FILM_SHARE_MAX, EditPlan
from engine.produce.ffmpeg import FfmpegError, MediaInfo
from engine.produce.process import Runner, output_tail, run_command

SILENCE_LUFS = -70.0  # ebur128's absolute gate: nothing above it to measure


@dataclass(frozen=True)
class AssetFacts:
    rights_type: str
    people_release: str
    media_kind: str


def parse_ebur128(stderr: str) -> tuple[float | None, float | None]:
    """Integrated loudness (LUFS) and true peak (dBTP) from an ebur128 summary; None for
    silence (a fake voice)."""
    summary = stderr[stderr.rfind("Summary:") :]
    integrated = re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", summary)
    peak = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
    if integrated is None or peak is None:
        raise FfmpegError("ebur128 printed no summary")
    lufs = float(integrated.group(1))
    true_peak = float(peak.group(1))
    return (
        None if lufs <= SILENCE_LUFS else lufs,
        None if true_peak == float("-inf") else true_peak,
    )


def measure_loudness(path: Path, run: Runner = run_command) -> tuple[float | None, float | None]:
    argv = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
            "-af", "ebur128=peak=true", "-f", "null", "-"]  # fmt: skip
    result = run(argv, None)
    if result.returncode != 0:
        raise FfmpegError(f"loudness measurement failed on {path}: {output_tail(result)}")
    return parse_ebur128(result.stderr)


BBOX = re.compile(r"x1:(-?\d+) x2:(-?\d+) y1:(-?\d+) y2:(-?\d+)")


def parse_bbox(stderr: str) -> list[int] | None:
    """The union of ffmpeg `bbox` boxes over the frames as [x1, y1, x2, y2]; None when no
    frame showed anything."""
    boxes = [tuple(map(int, m.groups())) for m in BBOX.finditer(stderr)]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[2] for b in boxes),
            max(b[1] for b in boxes), max(b[3] for b in boxes)]  # fmt: skip


def measure_text_box(overlay: Path, run: Runner = run_command) -> list[int] | None:
    """Where an alpha overlay draws anything, over the whole clip, sampled at 4 fps
    (FR-QA-4 safe zones)."""
    argv = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(overlay),
            "-vf", "fps=4,alphaextract,bbox=min_val=32", "-f", "null", "-"]  # fmt: skip
    result = run(argv, None)
    if result.returncode != 0:
        raise FfmpegError(f"caption box measurement failed on {overlay}: {output_tail(result)}")
    return parse_bbox(result.stderr)


def _seconds(value: float) -> float:
    return round(value, 3)


def build_render_report(
    plan: EditPlan,
    facts: Mapping[int, AssetFacts],
    info: MediaInfo,
    loudness: tuple[float | None, float | None],
    words: Sequence[Word],
    targets: tuple[float, float],
    caption_box: list[int] | None = None,
) -> dict:
    tiers: dict[str, float] = {}
    people: dict[str, list[int]] = {}
    film_longest = 0.0
    for shot in plan.timeline:
        fact = facts[shot.asset_id]
        tiers[fact.rights_type] = tiers.get(fact.rights_type, 0.0) + shot.duration
        if fact.media_kind != "audio" and shot.asset_id not in people.get(fact.people_release, []):
            people.setdefault(fact.people_release, []).append(shot.asset_id)
        if fact.rights_type == "film_reference":
            film_longest = max(film_longest, shot.out_s - shot.in_s, shot.duration)
    total = plan.duration or 1.0
    film = tiers.get("film_reference", 0.0)
    groups = group_words(words) if words else []
    return {
        "duration": _seconds(info.duration),
        "plan_duration": _seconds(plan.duration),
        "video": {
            "codec": info.video_codec,
            "width": info.width,
            "height": info.height,
            "fps": info.fps,
            "pix_fmt": info.pix_fmt,
        },
        "audio": {"codec": info.audio_codec, "sample_rate": info.sample_rate},
        "metadata_tags": sorted(info.tags),
        "loudness": {
            "integrated_lufs": loudness[0],
            "true_peak_dbtp": loudness[1],
            "target_lufs": targets[0],
            "target_true_peak_dbtp": targets[1],
        },
        "tiers": {tier: _seconds(s) for tier, s in sorted(tiers.items())},
        "tier_shares": {tier: round(s / total, 4) for tier, s in sorted(tiers.items())},
        "film": {
            "seconds": _seconds(film),
            "share": round(film / total, 4),
            "longest_excerpt_s": _seconds(film_longest),
            "limit_share": FILM_SHARE_MAX,
            "limit_excerpt_s": FILM_EXCERPT_MAX_S,
        },
        "people": {release: ids for release, ids in sorted(people.items())},
        "ai_generated_on_screen": any(
            facts[s.asset_id].rights_type == "ai_generated" for s in plan.timeline
        ),
        "captions": {
            "words": len(words),
            "groups": len(groups),
            "coverage": round(caption_coverage(words, groups), 4) if words else None,
            "box": caption_box,  # [x1, y1, x2, y2] drawn, or None
        },
    }
