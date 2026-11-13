"""Generated media for golden tests (NFR-9: fixtures are made by ffmpeg, never real footage),
plus the frame and audio measurements the tests compare."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def video(path: Path, source: str) -> Path:
    ffmpeg("-f", "lavfi", "-i", source, "-c:v", "libx264", "-crf", "12", "-pix_fmt", "yuv420p",
           str(path))  # fmt: skip
    return path


def tone(path: Path, frequency: int, seconds: float) -> Path:
    ffmpeg("-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration={seconds}",
           "-c:a", "pcm_s16le", str(path))  # fmt: skip
    return path


def alpha_box(path: Path, seconds: float, box: tuple[int, int, int, int], colour: str) -> Path:
    """A transparent 1080x1920 ProRes 4444 clip with one opaque box, like a HyperFrames block."""
    x, y, w, h = box
    # The alpha format must be set inside the source graph: a bare `color` input is
    # negotiated to a format without alpha before any -vf runs, and comes out opaque black.
    source = (
        f"color=c=black@0.0:s=1080x1920:r=30:d={seconds},format=yuva444p10le,"
        f"drawbox=x={x}:y={y}:w={w}:h={h}:color={colour}@1:t=fill:replace=1"
    )
    prores = ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le"]
    ffmpeg("-f", "lavfi", "-i", source, *prores, str(path))
    return path


def warm_lut(path: Path, size: int = 17) -> Path:
    """A 3D LUT that lifts red by 0.06 and scales blue by 0.82: grey turns visibly warm."""
    lines = ['TITLE "golden warm"', f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    step = 1 / (size - 1)
    for b in range(size):
        for g in range(size):
            for r in range(size):
                red, green, blue = min(1.0, r * step + 0.06), g * step, b * step * 0.82
                lines.append(f"{red:.6f} {green:.6f} {blue:.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def raw_frame(path: Path, at: float, vf: str, pix_fmt: str) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
         "-vf", vf, "-pix_fmt", pix_fmt, "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip


def thumbnail(path: Path, at: float) -> bytes:
    """A 32x56 grey thumbnail: a frame hash that tolerates encoder noise."""
    return raw_frame(path, at, "scale=32:56:flags=area", "gray")


def rgb_means(path: Path, at: float, box: tuple[int, int, int, int]) -> tuple[float, ...]:
    x, y, w, h = box
    data = raw_frame(path, at, f"crop={w}:{h}:{x}:{y}", "rgb24")
    count = len(data) // 3
    return tuple(sum(data[c::3]) / count for c in range(3))


def mean_abs_diff(a: bytes, b: bytes) -> float:
    assert len(a) == len(b) > 0
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def band_rms_db(path: Path, start: float, seconds: float, frequency: int) -> float:
    """RMS level (dBFS) of a narrow band around `frequency` over a time window."""
    band = f"bandpass=f={frequency}:width_type=h:w=20"
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", str(start), "-t", str(seconds),
         "-i", str(path), "-af", f"{band},{band},astats=metadata=0", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return float(re.findall(r"RMS level dB: (-?[\d.]+|-inf)", result.stderr)[-1])


def loudness(path: Path) -> tuple[float, float]:
    """Integrated loudness (LUFS) and true peak (dBTP) from ebur128."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true",
         "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    summary = result.stderr[result.stderr.rfind("Summary:") :]
    integrated = float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1))
    peak = float(re.search(r"True peak:\s+Peak:\s+(-?[\d.]+) dBFS", summary).group(1))
    return integrated, peak
