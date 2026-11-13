"""Render an edit plan with ffmpeg (SRD §7.2, FR-PRD-5). Choices are in ADR 0008.

Three ffmpeg runs, each reading its filter graph from a file (`-/filter_complex <file>`),
so a long timeline never meets the Windows command-line cap:

1. mix: VO segments, the music bed ducked under the VO by a sidechain compressor with a
   fixed floor (the plan's `duck_db`), and SFX hits, to `mix.wav`;
2. measure: loudnorm pass 1 on the mix;
3. encode: shot chains (trim, crop, speed, fps, cover fit, freeze, eased zoom), concat,
   LUT grade, HyperFrames overlays and captions on top, loudnorm pass 2 (linear) on the mix,
   NVENC H.264 + AAC 48 kHz, metadata stripped.

The graph builders are pure functions so they are unit-tested without ffmpeg (NFR-8).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from engine.produce.editplan import EditPlan, Shot
from engine.produce.ffmpeg import METADATA_STRIP, FfmpegError
from engine.produce.process import Runner, output_tail, run_command

if TYPE_CHECKING:
    from engine.config import EngineConfig

LOUDNESS_RANGE = 20  # loose LRA target so pass 2 stays linear (one constant gain)
SIDECHAIN = "sidechaincompress=threshold=0.001:ratio=20:attack=20:release=300"
AUDIO_FORMAT = "aformat=sample_fmts=fltp:channel_layouts=stereo"


class RenderError(ValueError):
    """The plan and its sources cannot be turned into an ffmpeg job."""


@dataclass(frozen=True)
class RenderSettings:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    sample_rate: int = 48000
    lufs: float = -14.0
    true_peak: float = -1.0
    encoder: str = "h264_nvenc"

    @classmethod
    def from_engine(cls, engine: EngineConfig) -> RenderSettings:
        return cls(
            width=engine.render.width,
            height=engine.render.height,
            fps=engine.render.fps,
            sample_rate=engine.render.audio_sample_rate,
            lufs=float(engine.loudness.integrated_lufs),
            true_peak=float(engine.loudness.true_peak_dbtp),
        )


@dataclass(frozen=True)
class RenderSources:
    """Files for everything the plan names: asset ids, overlay block ids, captions, the LUT."""

    assets: Mapping[int, Path]
    overlays: Mapping[str, Path] = field(default_factory=dict)
    captions: Path | None = None
    lut: Path | None = None

    def asset(self, asset_id: int) -> Path:
        if asset_id not in self.assets:
            raise RenderError(f"asset {asset_id} has no file")
        return self.assets[asset_id]

    def overlay(self, block: str) -> Path:
        if block not in self.overlays:
            raise RenderError(f"overlay block '{block}' has no rendered file")
        return self.overlays[block]


@dataclass(frozen=True)
class RenderResult:
    path: Path
    duration: float
    measured: dict[str, str]  # loudnorm pass 1 on the mix


def num(value: float) -> str:
    """A number as ffmpeg likes it: no trailing zeros, no exponent."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def filter_path(path: Path) -> str:
    """A path quoted for a filter option: forward slashes, escaped drive colon and quotes."""
    text = str(path).replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")
    return f"'{text}'"


def duck_floor_gain(duck_db: float) -> float:
    """Share of dry music mixed back in so the ducked bed sits exactly `duck_db` lower.

    The compressed copy falls to near silence under VO; adding the dry copy at `k` and
    normalising by `1 + k` leaves the unducked level unchanged and the ducked level at
    `k / (1 + k)`, which is `-duck_db` dB.
    """
    return 1 / (10 ** (duck_db / 20) - 1)


def _ease(ease: str, progress: str) -> str:
    return {
        "linear": progress,
        "in": f"pow({progress},2)",
        "out": f"(1-pow(1-{progress},2))",
        "in_out": f"(1-cos(PI*{progress}))/2",
    }[ease]


def shot_chain(shot: Shot, index: int, label: str, settings: RenderSettings) -> str:
    w, h, fps = settings.width, settings.height, settings.fps
    ops = shot.ops
    parts = [f"trim=start={num(shot.in_s)}:end={num(shot.out_s)}", "setpts=PTS-STARTPTS"]
    if ops.crop:
        parts.append(f"crop={ops.crop.w}:{ops.crop.h}:{ops.crop.x}:{ops.crop.y}")
    if ops.speed != 1:
        parts.append(f"setpts=PTS/{num(ops.speed)}")
    played_frames = max(round(shot.played * fps), 1)
    parts += [
        f"fps={fps}",
        f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1",
        # pad then cut to an exact frame count so shot lengths add up to the plan
        f"tpad=stop_mode=clone:stop=2,trim=end_frame={played_frames},setpts=N/{fps}/TB",
    ]
    if ops.freeze:
        start = min(round(ops.freeze.at * fps), played_frames - 1)
        hold = round(ops.freeze.duration * fps)
        parts.append(f"loop=loop={hold}:size=1:start={start},setpts=N/{fps}/TB")
    if ops.zoom and ops.zoom.to_scale != ops.zoom.from_scale:
        last = max(round(shot.duration * fps) - 1, 1)
        delta = num(ops.zoom.to_scale - ops.zoom.from_scale)
        z = f"{num(ops.zoom.from_scale)}+{delta}*{_ease(ops.zoom.ease, f'min(on/{last},1)')}"
        parts.append(
            f"scale={2 * w}:{2 * h},zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:s={w}x{h}:fps={fps}"
        )
    parts.append("format=yuv420p")
    return f"[{index}:v]" + ",".join(parts) + f"[{label}]"


def video_graph(
    plan: EditPlan, sources: RenderSources, settings: RenderSettings
) -> tuple[str, list[Path]]:
    """The video filter graph ending in `[vout]`, and its input files in index order."""
    inputs = [sources.asset(shot.asset_id) for shot in plan.timeline]
    chains = [shot_chain(shot, i, f"v{i}", settings) for i, shot in enumerate(plan.timeline)]
    labels = "".join(f"[v{i}]" for i in range(len(plan.timeline)))
    chains.append(f"{labels}concat=n={len(plan.timeline)}:v=1:a=0[vcat]")
    current = "vcat"
    if sources.lut is not None:
        chains.append(f"[vcat]lut3d=file={filter_path(sources.lut)}:interp=tetrahedral[vg]")
        current = "vg"
    layers = [(sources.overlay(o.block), o.start, o.end) for o in plan.placed_overlays()]
    if plan.captions is not None:
        if sources.captions is None:
            raise RenderError(f"captions block '{plan.captions.block}' has no rendered file")
        layers.append((sources.captions, 0.0, plan.duration))
    for j, (path, start, end) in enumerate(layers):
        index = len(inputs)
        inputs.append(path)
        chains.append(f"[{index}:v]setpts=PTS-STARTPTS+{num(start)}/TB[ov{j}]")
        chains.append(
            f"[{current}][ov{j}]overlay=0:0:format=auto:eof_action=pass"
            f":enable='between(t,{num(start)},{num(end)})'[c{j}]"
        )
        current = f"c{j}"
    chains.append(f"[{current}]format=yuv420p[vout]")
    return ";\n".join(chains), inputs


def audio_graph(
    plan: EditPlan, sources: RenderSources, settings: RenderSettings
) -> tuple[str, list[tuple[list[str], Path]]]:
    """The audio mix graph ending in `[aout]`, and its inputs as (input options, file)."""
    total = num(plan.duration)
    sr = settings.sample_rate
    audio = plan.audio
    inputs: list[tuple[list[str], Path]] = []
    chains: list[str] = []
    parts: list[str] = []

    voice_labels = []
    for j, segment in enumerate(audio.voice):
        inputs.append(([], sources.asset(segment.asset_id)))
        delay = round(segment.at * 1000)
        chains.append(
            f"[{len(inputs) - 1}:a]atrim=start={num(segment.in_s)}:end={num(segment.out_s)},"
            f"asetpts=PTS-STARTPTS,aresample={sr},{AUDIO_FORMAT},adelay={delay}:all=1[vo{j}]"
        )
        voice_labels.append(f"[vo{j}]")
    voice = None
    if voice_labels:
        mixed = (
            f"amix=inputs={len(voice_labels)}:normalize=0:duration=longest"
            if len(voice_labels) > 1
            else "anull"
        )
        chains.append(f"{''.join(voice_labels)}{mixed}[vo]")
        voice = "[vo]"

    if audio.music is not None:
        inputs.append((["-stream_loop", "-1"], sources.asset(audio.music.asset_id)))
        chains.append(
            f"[{len(inputs) - 1}:a]aresample={sr},{AUDIO_FORMAT},atrim=0:{total},"
            f"asetpts=PTS-STARTPTS,volume={num(audio.music.gain_db)}dB[mus]"
        )
        if voice and audio.music.duck_db > 0:
            k = duck_floor_gain(audio.music.duck_db)
            chains += [
                "[vo]asplit=2[vomix][vosc]",
                "[mus]asplit=2[mdry][mkey]",
                f"[mkey][vosc]{SIDECHAIN}[mduck]",
                f"[mdry]volume={k:.6f}[mfloor]",
                f"[mduck][mfloor]amix=inputs=2:normalize=0,volume={1 / (1 + k):.6f}[mbed]",
            ]
            parts += ["[vomix]", "[mbed]"]
        else:
            parts += ([voice] if voice else []) + ["[mus]"]
    elif voice:
        parts.append(voice)

    for j, hit in enumerate(audio.sfx):
        inputs.append(([], sources.asset(hit.asset_id)))
        chains.append(
            f"[{len(inputs) - 1}:a]aresample={sr},{AUDIO_FORMAT},"
            f"adelay={round(hit.at * 1000)}:all=1,volume={num(hit.gain_db)}dB[sx{j}]"
        )
        parts.append(f"[sx{j}]")

    if not parts:
        return f"anullsrc=r={sr}:cl=stereo,atrim=0:{total}[aout]", []
    head = f"amix=inputs={len(parts)}:normalize=0:duration=longest," if len(parts) > 1 else ""
    chains.append(f"{''.join(parts)}{head}apad=whole_dur={total},atrim=0:{total}[aout]")
    return ";\n".join(chains), inputs


def parse_loudnorm(stderr: str) -> dict[str, str]:
    start = stderr.rfind("{")
    end = stderr.find("}", start)
    if start < 0 or end < 0:
        raise FfmpegError("loudnorm printed no measurement")
    return json.loads(stderr[start : end + 1])


def loudnorm_filter(measured: Mapping[str, str], settings: RenderSettings) -> str | None:
    """Pass-2 loudnorm with the pass-1 numbers, or None for a silent mix."""
    if not math.isfinite(float(measured["input_i"])):
        return None
    return (
        f"loudnorm=I={settings.lufs}:TP={settings.true_peak}:LRA={LOUDNESS_RANGE}"
        f":measured_I={measured['input_i']}:measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}:linear=true,aresample={settings.sample_rate}"
    )


def _encoder_args(encoder: str) -> list[str]:
    if encoder.endswith("_nvenc"):
        return ["-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0"]
    return ["-preset", "medium", "-crf", "20"]


def _checked(result, stage: str):
    if result.returncode != 0:
        raise FfmpegError(
            f"ffmpeg {stage} failed (exit {result.returncode}): {output_tail(result)}"
        )
    return result


def mix_audio(
    plan: EditPlan,
    sources: RenderSources,
    work_dir: Path,
    settings: RenderSettings = RenderSettings(),
    run: Runner = run_command,
) -> Path:
    """The plan's final audio mix (VO, ducked music, SFX) as `work_dir/mix.wav`.

    Captions are transcribed from this file (FR-PRD-6) before the encode reuses it.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    graph, inputs = audio_graph(plan, sources, settings)
    audio_file = work_dir / "audio.graph"
    audio_file.write_text(graph, encoding="utf-8")
    mix = work_dir / "mix.wav"
    command = ["ffmpeg", "-v", "error", "-y"]
    for options, path in inputs:
        command += [*options, "-i", str(path)]
    command += ["-/filter_complex", str(audio_file), "-map", "[aout]"]
    command += ["-c:a", "pcm_s24le", "-ar", str(settings.sample_rate), str(mix)]
    _checked(run(command, None), "mix")
    return mix


def render(
    plan: EditPlan,
    sources: RenderSources,
    output: Path,
    work_dir: Path,
    settings: RenderSettings = RenderSettings(),
    run: Runner = run_command,
    mix: Path | None = None,
) -> RenderResult:
    """Render the plan to `output`, reusing `mix` when the caller already made it."""
    work_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    sr = str(settings.sample_rate)
    if mix is None:
        mix = mix_audio(plan, sources, work_dir, settings, run)

    loudnorm_pass_one = (
        f"loudnorm=I={settings.lufs}:TP={settings.true_peak}:LRA={LOUDNESS_RANGE}:print_format=json"
    )
    measure = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(mix)]
    measure += ["-af", loudnorm_pass_one, "-f", "null", "-"]
    measured = parse_loudnorm(_checked(run(measure, None), "measure").stderr)

    graph, inputs = video_graph(plan, sources, settings)
    normalise = loudnorm_filter(measured, settings) or "anull"
    graph += f";\n[{len(inputs)}:a]{normalise}[aout]"
    video_file = work_dir / "video.graph"
    video_file.write_text(graph, encoding="utf-8")
    command = ["ffmpeg", "-v", "error", "-y"]
    for path in [*inputs, mix]:
        command += ["-i", str(path)]
    command += ["-/filter_complex", str(video_file), "-map", "[vout]", "-map", "[aout]"]
    command += ["-c:v", settings.encoder, *_encoder_args(settings.encoder)]
    command += ["-pix_fmt", "yuv420p", "-r", str(settings.fps)]
    command += ["-c:a", "aac", "-b:a", "192k", "-ar", sr, "-t", num(plan.duration)]
    command += [*METADATA_STRIP, "-movflags", "+faststart", str(output)]
    _checked(run(command, None), "encode")
    return RenderResult(path=output, duration=plan.duration, measured=measured)
