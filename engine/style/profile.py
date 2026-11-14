"""`ce refs analyze`: measure each reference short, aggregate the numbers into one style
profile per identity, store it, then delete the references (SRD §9.1, C10).

Only numbers and the palette are stored; the file names are not. A reference is deleted
only after the profile that used it is stored, so a failure never loses unmeasured work.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict

from engine.library.transcribe import Transcriber
from engine.produce.ffmpeg import probe
from engine.produce.process import Runner, run_command
from engine.style.measure import (
    brightness_saturation,
    cut_command,
    loudness_command,
    palette,
    parse_loudness_range,
    parse_scene_cuts,
    shot_stats,
    tempo_bpm,
    text_stats,
)
from engine.style.ocr import TextDetector

VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
FRAME_FPS = 2.0  # sampled frames a second, for text, palette and light
FRAME_WIDTH = 540  # OCR needs caption text a few dozen pixels tall
AUDIO_RATE = 11025
Reader = Callable[[list[str]], bytes]


class RefMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file: str
    duration: float
    width: int
    height: int
    shots: int
    cuts_per_min: float
    shot_median_s: float
    shot_p10_s: float
    shot_p90_s: float
    first_word_s: float | None
    words_per_s: float | None
    first_text_s: float | None
    text_frame_share: float
    caption_y: float | None
    caption_height: float | None
    words_per_page: int | None
    loudness_lufs: float | None
    loudness_range: float | None
    tempo_bpm: float | None
    brightness: float
    saturation: float
    palette: list[str]


def read_bytes(argv: list[str]) -> bytes:
    return subprocess.run(argv, capture_output=True, check=True).stdout


@dataclass
class MeasureDeps:
    detector: TextDetector
    transcriber: Transcriber | None = None  # None: no speech timings
    run: Runner = run_command
    read: Reader = read_bytes


def reference_files(folder: Path) -> list[Path]:
    return sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES),
        key=lambda p: p.name.lower(),
    )


def _frames(video: Path, width: int, height: int, read: Reader) -> np.ndarray:
    h = round(height * FRAME_WIDTH / width / 2) * 2
    raw = read(["ffmpeg", "-v", "error", "-i", str(video), "-vf",
                f"fps={FRAME_FPS},scale={FRAME_WIDTH}:{h}", "-f", "rawvideo",
                "-pix_fmt", "rgb24", "-"])  # fmt: skip
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, h, FRAME_WIDTH, 3)


def _samples(video: Path, read: Reader) -> np.ndarray:
    raw = read(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar",
                str(AUDIO_RATE), "-f", "s16le", "-"])  # fmt: skip
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768


def _speech(video: Path, transcriber: Transcriber | None, run: Runner) -> tuple:
    if transcriber is None:
        return None, None
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "speech.wav"
        result = run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1",
                      "-ar", "16000", str(wav)], None)  # fmt: skip
        if result.returncode != 0 or not wav.is_file():
            return None, None
        words = transcriber.transcribe(wav).words
    if not words:
        return None, None
    span = max(words[-1].end - words[0].start, 0.1)
    return round(words[0].start, 3), round(len(words) / span, 3)


def measure_reference(video: Path, deps: MeasureDeps) -> RefMetrics:
    info = probe(video, deps.run)
    cuts = parse_scene_cuts(deps.run(cut_command(video), None).stderr)
    frames = _frames(video, info.width, info.height, deps.read)
    texts = [(i / FRAME_FPS, deps.detector.detect(f)) for i, f in enumerate(frames)]
    text = text_stats(
        [(t, boxes) for t, boxes in texts], width=FRAME_WIDTH, height=frames.shape[1]
    ) if len(frames) else text_stats([], FRAME_WIDTH, 1)  # fmt: skip
    pixels = frames[:, ::4, ::4].reshape(-1, 3) if len(frames) else np.zeros((1, 3), np.uint8)
    light = brightness_saturation(pixels)
    has_audio = info.audio_codec is not None
    lufs, lra = (
        parse_loudness_range(deps.run(loudness_command(video), None).stderr)
        if has_audio
        else (None, None)
    )
    first_word, rate = _speech(video, deps.transcriber, deps.run) if has_audio else (None, None)
    return RefMetrics(
        file=video.name,
        duration=round(info.duration, 3),
        width=info.width,
        height=info.height,
        **shot_stats(cuts, info.duration),
        first_word_s=first_word,
        words_per_s=rate,
        **text,
        loudness_lufs=lufs,
        loudness_range=lra,
        tempo_bpm=tempo_bpm(_samples(video, deps.read), AUDIO_RATE) if has_audio else None,
        brightness=light[0],
        saturation=light[1],
        palette=palette(pixels),
    )


def aggregate(refs: Sequence[RefMetrics]) -> dict:
    """Medians across references (missing values skipped) and a palette of their palettes."""
    numeric = [name for name, f in RefMetrics.model_fields.items()
               if name not in ("file", "palette")]  # fmt: skip
    metrics = {}
    for name in numeric:
        values = [getattr(r, name) for r in refs if getattr(r, name) is not None]
        metrics[name] = round(float(np.median(values)), 4) if values else None
    swatches = [s for r in refs for s in r.palette]
    pixels = np.array([[int(s[i : i + 2], 16) for i in (1, 3, 5)] for s in swatches], np.uint8)
    return {"metrics": metrics, "palette": palette(pixels) if len(pixels) else [],
            "ref_count": len(refs)}  # fmt: skip


def store_profile(conn: sqlite3.Connection, identity: str, profile: dict) -> int:
    return conn.execute(
        "INSERT INTO style_profiles (identity_id, measured_at, metrics, palette, ref_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (identity, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
         json.dumps(profile["metrics"]), json.dumps(profile["palette"]), profile["ref_count"]),
    ).lastrowid  # fmt: skip


def analyze_refs(
    conn: sqlite3.Connection,
    identity: str,
    folder: Path,
    measure: Callable[[Path], RefMetrics],
    keep: bool = False,
) -> dict:
    files = reference_files(folder) if folder.is_dir() else []
    if not files:
        raise ValueError(
            f"no reference videos in {folder}: drop 5-10 shorts you admire there first (G0.8)"
        )
    profile = aggregate([measure(f) for f in files])
    profile["profile_id"] = store_profile(conn, identity, profile)
    if not keep:
        for f in files:  # C10: only the numbers stay
            f.unlink()
    return profile
