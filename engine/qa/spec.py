"""FR-QA-4 spec check: what the render report measured against `render:`, `loudness:` and
`qa:` in engine.yaml. ADR 0018.

- Resolution and fps match `render:` exactly.
- Duration is inside the format and within 0.2 s of the plan.
- Integrated loudness is within `qa.loudness_tolerance_lu` of the target, and true peak is
  at most its target.
- Speech carries captions covering at least `qa.caption_coverage_min` of it.
- The measured caption box stays inside every target platform's safe zone.

A silent mix fails: speech that measures as silence is a broken render. The one exception is
a voice provider that is silent by design (the dry run's fake voice).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from engine.agents.schemas import FORMAT_SECONDS
from engine.config.models import EngineConfig, SafeZone
from engine.qa import CheckResult, verdict

DURATION_SLACK_S = 0.2
FPS_SLACK = 0.01
PEAK_SLACK_DB = 0.05


@dataclass(frozen=True)
class SpecRules:
    width: int
    height: int
    fps: float
    seconds: tuple[float, float]
    lufs: float
    true_peak: float
    tolerance_lu: float
    coverage_min: float
    safe_zones: Mapping[str, SafeZone]

    @classmethod
    def from_engine(cls, engine: EngineConfig, fmt: str) -> SpecRules:
        return cls(
            width=engine.render.width,
            height=engine.render.height,
            fps=float(engine.render.fps),
            seconds=FORMAT_SECONDS[fmt],
            lufs=float(engine.loudness.integrated_lufs),
            true_peak=float(engine.loudness.true_peak_dbtp),
            tolerance_lu=engine.qa.loudness_tolerance_lu,
            coverage_min=engine.qa.caption_coverage_min,
            safe_zones=engine.qa.safe_zones,
        )


def _zone_problems(box: Sequence[int], zone: SafeZone, platform: str, rules: SpecRules) -> list:
    x1, y1, x2, y2 = box
    sides = [
        ("top", y1 < zone.top, f"y={y1}", zone.top),
        ("bottom", y2 >= rules.height - zone.bottom, f"y={y2}", zone.bottom),
        ("left", x1 < zone.left, f"x={x1}", zone.left),
        ("right", x2 >= rules.width - zone.right, f"x={x2}", zone.right),
    ]
    return [
        f"captions reach {where}, inside {platform}'s {side} UI ({px} px) (FR-QA-4)"
        for side, outside, where, px in sides
        if outside
    ]


def spec_check(
    report: Mapping | None,
    rules: SpecRules,
    platforms: Sequence[str],
    *,
    speech: bool,
    captions: bool,
    silent_voice: bool,
) -> CheckResult:
    if not report or "video" not in report:
        return verdict("spec", ["the render has no report to check (FR-QA-4)"])
    fails: list[str] = []
    holds: list[str] = []
    video = report["video"]
    if (video.get("width"), video.get("height")) != (rules.width, rules.height):
        fails.append(
            f"resolution {video.get('width')}x{video.get('height')}, not "
            f"{rules.width}x{rules.height}"
        )
    if video.get("fps") is None or abs(video["fps"] - rules.fps) > FPS_SLACK:
        fails.append(f"{video.get('fps')} fps, not {rules.fps:g}")
    low, high = rules.seconds
    duration = report.get("duration") or 0.0
    if not low <= duration <= high:
        fails.append(f"runs {duration:g} s, outside {low:g}-{high:g} s")
    if abs(duration - (report.get("plan_duration") or 0.0)) > DURATION_SLACK_S:
        fails.append(f"runs {duration:g} s but the plan runs {report.get('plan_duration')} s")

    loudness = report.get("loudness") or {}
    lufs, peak = loudness.get("integrated_lufs"), loudness.get("true_peak_dbtp")
    if lufs is None:
        if not silent_voice:
            fails.append("the mix measures as silence")
    elif abs(lufs - rules.lufs) > rules.tolerance_lu:
        fails.append(f"loudness {lufs:g} LUFS, outside {rules.lufs:g} ± {rules.tolerance_lu:g} LU")
    if peak is not None and peak > rules.true_peak + PEAK_SLACK_DB:
        fails.append(f"true peak {peak:g} dBTP, above {rules.true_peak:g}")

    caption = report.get("captions") or {}
    if speech and not captions:
        fails.append("speech without captions")
    if speech and captions:
        coverage = caption.get("coverage")
        if coverage is None or coverage < rules.coverage_min:
            shown = "none" if coverage is None else f"{coverage:.2f}"
            fails.append(f"captions cover {shown} of the speech, below {rules.coverage_min:.2f}")
    if captions:
        box = caption.get("box")
        if box is None:
            fails.append("captions planned but nothing visible on the caption overlay")
        else:
            for platform in platforms:
                zone = rules.safe_zones.get(platform)
                if zone is None:
                    holds.append(f"no safe zone configured for {platform} (engine.yaml qa)")
                else:
                    fails += _zone_problems(box, zone, platform, rules)
    tagged = [f if f.endswith("(FR-QA-4)") else f"{f} (FR-QA-4)" for f in fails]
    return verdict("spec", tagged, holds)
