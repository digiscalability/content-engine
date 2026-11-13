"""ffmpeg compositing and ffprobe checks (SRD §7.2, FR-PRD-5).

HyperFrames overlays arrive as ProRes 4444 MOV with alpha (ADR 0001), which ffmpeg's
default decoder reads with its alpha channel, so `overlay` needs no decoder flags.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from engine.produce.process import Runner, output_tail, run_command

OUTPUT_FPS = 30  # FR-PRD-5
OUTPUT_SAMPLE_RATE = 48000  # FR-PRD-5
# FR-PRD-5 "metadata stripped": -map_metadata alone leaves the muxer's encoder tags (ADR 0001).
METADATA_STRIP = [
    "-map_metadata", "-1",
    "-map_chapters", "-1",
    "-metadata:s:v", "encoder=",
    "-fflags", "+bitexact",
]  # fmt: skip


class FfmpegError(RuntimeError):
    """ffmpeg or ffprobe exited non-zero."""


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    pix_fmt: str
    audio_codec: str | None = None
    sample_rate: int | None = None
    tags: dict[str, str] = field(default_factory=dict)


def probe(path: Path, run: Runner = run_command) -> MediaInfo:
    """Duration, first video stream, first audio stream and every tag, via ffprobe."""
    argv = [
        "ffprobe", "-v", "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,r_frame_rate,pix_fmt,sample_rate"
        ":stream_tags:format=duration:format_tags",
        "-of", "json", str(path),
    ]  # fmt: skip
    result = run(argv, None)
    if result.returncode != 0:
        raise FfmpegError(f"ffprobe failed on {path}: {output_tail(result)}")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next(s for s in streams if s.get("codec_type") == "video")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    tags: dict[str, str] = dict(data.get("format", {}).get("tags", {}))
    for stream in streams:
        tags.update(stream.get("tags", {}))
    return MediaInfo(
        duration=float(data["format"]["duration"]),
        width=int(video["width"]),
        height=int(video["height"]),
        fps=float(Fraction(video["r_frame_rate"])),
        video_codec=video["codec_name"],
        pix_fmt=video["pix_fmt"],
        audio_codec=audio["codec_name"] if audio else None,
        sample_rate=int(audio["sample_rate"]) if audio else None,
        tags=tags,
    )


def wav_duration(path: Path) -> float:
    """Seconds of audio in a PCM WAV, from its header: works for the 24-bit
    WAVE_FORMAT_EXTENSIBLE files ffmpeg writes, which the `wave` module rejects on 3.11."""
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise FfmpegError(f"{path} is not a WAV file")
    offset, byte_rate = 12, 0
    while offset + 8 <= len(data):
        chunk, size = (
            data[offset : offset + 4],
            struct.unpack("<I", data[offset + 4 : offset + 8])[0],
        )
        if chunk == b"fmt ":
            byte_rate = struct.unpack("<I", data[offset + 16 : offset + 20])[0]
        elif chunk == b"data":
            if not byte_rate:
                raise FfmpegError(f"{path} has its data before its format")
            return round(size / byte_rate, 6)
        offset += 8 + size + (size & 1)
    raise FfmpegError(f"{path} has no data chunk")


def composite_command(
    base: Path,
    overlay: Path,
    output: Path,
    encoder: str = "h264_nvenc",
    fps: int = OUTPUT_FPS,
    sample_rate: int = OUTPUT_SAMPLE_RATE,
) -> list[str]:
    """Overlay an alpha video on a base clip; H.264, AAC 48 kHz, metadata stripped."""
    return [
        "ffmpeg", "-v", "error", "-y",
        "-i", str(base),
        "-i", str(overlay),
        "-filter_complex", "[0:v][1:v]overlay=0:0:format=auto,format=yuv420p[v]",
        "-map", "[v]",
        "-map", "0:a?",  # the base clip's audio if it has any
        "-c:v", encoder,
        "-r", str(fps),
        "-c:a", "aac",
        "-ar", str(sample_rate),
        *METADATA_STRIP,
        "-movflags", "+faststart",
        str(output),
    ]  # fmt: skip


def composite_overlay(
    base: Path,
    overlay: Path,
    output: Path,
    encoder: str = "h264_nvenc",
    run: Runner = run_command,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    result = run(composite_command(base, overlay, output, encoder=encoder), None)
    if result.returncode != 0:
        raise FfmpegError(
            f"ffmpeg composite failed (exit {result.returncode}): {output_tail(result)}"
        )
    return output
