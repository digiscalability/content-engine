"""What `ce refs analyze` measures in a reference short (SRD §9.1), as pure functions over
ffmpeg output and pixel or sample arrays, so each is unit-tested without media (NFR-8).

Cuts come from ffmpeg's scene score; the palette is a small deterministic k-means; tempo
is the strongest beat period of an onset envelope; text statistics come from the boxes a
`TextDetector` finds on sampled frames. Choices are in ADR 0015.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCENE_THRESHOLD = 0.3
CAPTION_TOP = 0.4  # a box whose centre is below 40% of the height counts as a caption
_PTS = re.compile(r"Parsed_showinfo.*?pts_time:\s*([\d.]+)")


@dataclass(frozen=True)
class TextBox:
    x: int
    y: int
    w: int
    h: int
    text: str


def cut_command(video: Path, threshold: float = SCENE_THRESHOLD) -> list[str]:
    scene = f"select='gt(scene,{threshold})',showinfo"
    return ["ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
            "-vf", scene, "-an", "-f", "null", "-"]  # fmt: skip


def parse_scene_cuts(stderr: str) -> list[float]:
    return [float(t) for t in _PTS.findall(stderr)]


def shot_stats(cuts: Sequence[float], duration: float) -> dict:
    bounds = [0.0, *sorted(c for c in cuts if 0 < c < duration), duration]
    lengths = np.diff(bounds)
    return {
        "shots": len(lengths),
        "cuts_per_min": round((len(bounds) - 2) / duration * 60, 3) if duration else 0.0,
        "shot_median_s": round(float(np.median(lengths)), 3),
        "shot_p10_s": round(float(np.percentile(lengths, 10)), 3),
        "shot_p90_s": round(float(np.percentile(lengths, 90)), 3),
    }


def palette(pixels: np.ndarray, k: int = 5, iterations: int = 12) -> list[str]:
    """Up to `k` swatches (hex), most common first. Initial centres sit at luminance
    quantiles, so the same pixels always give the same palette."""
    points = pixels.reshape(-1, 3).astype(np.float64)
    if len(points) > 20_000:
        points = points[:: len(points) // 20_000 + 1]
    luminance = points @ np.array([0.2126, 0.7152, 0.0722])
    order = np.argsort(luminance, kind="stable")
    quantiles = [(i + 0.5) / k for i in range(k)]
    centres = points[order[[int(q * (len(points) - 1)) for q in quantiles]]]
    for _ in range(iterations):
        distances = ((points[:, None, :] - centres[None, :, :]) ** 2).sum(axis=2)
        labels = distances.argmin(axis=1)
        centres = np.array(
            [points[labels == i].mean(axis=0) if (labels == i).any() else centres[i]
             for i in range(len(centres))]
        )  # fmt: skip
    counts = np.bincount(labels, minlength=len(centres))
    swatches: dict[str, int] = {}
    for i in np.argsort(-counts, kind="stable"):
        if counts[i] == 0:
            continue
        r, g, b = (int(round(c)) for c in centres[i])
        key = f"#{r:02x}{g:02x}{b:02x}"
        swatches[key] = swatches.get(key, 0) + int(counts[i])
    return [s for s, _ in sorted(swatches.items(), key=lambda kv: -kv[1])][:k]


def brightness_saturation(pixels: np.ndarray) -> tuple[float, float]:
    """Mean HSV value and saturation, each 0 to 1."""
    rgb = pixels.reshape(-1, 3).astype(np.float64) / 255
    high, low = rgb.max(axis=1), rgb.min(axis=1)
    saturation = np.where(high > 0, (high - low) / np.where(high > 0, high, 1), 0.0)
    return round(float(high.mean()), 4), round(float(saturation.mean()), 4)


def tempo_bpm(samples: np.ndarray, rate: int, hop: int = 128) -> float | None:
    """The beat (60-180 bpm) of the onset envelope's strongest period; None for silence.
    Of near-equal peaks the fastest wins, so a half-tempo echo is not reported."""
    frames = len(samples) // hop
    if frames < 8:
        return None
    energy = np.sqrt((samples[: frames * hop].reshape(frames, hop) ** 2).mean(axis=1))
    if energy.max() < 1e-4:
        return None
    onset = np.maximum(np.diff(energy), 0)
    for _ in range(2):  # smoothing keeps a beat that falls between frames from splitting
        onset = np.convolve(onset, [0.25, 0.5, 0.25], mode="same")
    onset -= onset.mean()
    fps = rate / hop
    lags = np.arange(int(fps * 60 / 180), int(fps * 60 / 60) + 1)
    corr = np.array([float(np.dot(onset[:-lag], onset[lag:])) for lag in lags])
    if corr.max() <= 0:
        return None
    peaks = [
        i for i in range(1, len(corr) - 1) if corr[i] >= corr[i - 1] and corr[i] >= corr[i + 1]
    ]
    strong = [i for i in peaks if corr[i] >= 0.8 * corr.max()] or [int(corr.argmax())]
    i = min(strong)  # the shortest lag: the fastest strong beat
    shift = 0.0
    if 0 < i < len(corr) - 1:  # parabolic refinement between neighbouring lags
        a, b, c = corr[i - 1], corr[i], corr[i + 1]
        denominator = a - 2 * b + c
        shift = 0.5 * (a - c) / denominator if denominator else 0.0
    return round(60 * fps / (lags[i] + shift), 1)


LETTERS_PER_WORD = 5  # when OCR drops the spaces (it does on all-caps over colour)


def word_count(text: str) -> int:
    """Words in an OCR'd line: counted by spaces when it has them, else estimated from its
    letters, because PP-OCR often reads "START SMALL" as "STARTSMALL"."""
    if " " in text.strip():
        return len(text.split())
    letters = sum(ch.isalnum() for ch in text)
    return max(1, round(letters / LETTERS_PER_WORD)) if letters else 0


def text_stats(frames: Sequence[tuple[float, Sequence[TextBox]]], width: int, height: int) -> dict:
    """How often text is on screen, when it first appears, and where captions sit, how big
    they are and how many words a caption page shows."""
    with_text = [(t, boxes) for t, boxes in frames if boxes]
    pages = [[b for b in boxes if (b.y + b.h / 2) / height > CAPTION_TOP] for _, boxes in with_text]
    captions = [b for page in pages for b in page]
    words = [sum(word_count(b.text) for b in page) for page in pages if page]
    return {
        "text_frame_share": round(len(with_text) / len(frames), 4) if frames else 0.0,
        "first_text_s": with_text[0][0] if with_text else None,
        "caption_y": round(float(np.median([(b.y + b.h / 2) / height for b in captions])), 4)
        if captions
        else None,
        "caption_height": round(float(np.median([b.h / height for b in captions])), 4)
        if captions
        else None,
        "words_per_page": int(np.median(words)) if words else None,
    }


SILENCE_LUFS = -70.0  # ebur128's absolute gate: below it there is nothing to measure


def loudness_command(video: Path) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
            "-af", "ebur128", "-f", "null", "-"]  # fmt: skip


def parse_loudness_range(stderr: str) -> tuple[float | None, float | None]:
    """Integrated loudness (LUFS) and loudness range (LU) from an ebur128 summary; None
    for silence."""
    summary = stderr[stderr.rfind("Summary:") :]
    integrated = re.search(r"I:\s+(-?[\d.]+) LUFS", summary)
    spread = re.search(r"LRA:\s+(-?[\d.]+) LU", summary)
    if integrated is None or float(integrated.group(1)) <= SILENCE_LUFS:
        return None, None
    return float(integrated.group(1)), float(spread.group(1)) if spread else None
