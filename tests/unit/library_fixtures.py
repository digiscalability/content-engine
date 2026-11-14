"""Generated fixture footage for the library tests (NFR-9: drawn here, never real clips).

Each still is drawn to look like its subject to the real CLIP and to be dominated by the
colour the fake CLIP links to its words (sunrise orange, ocean blue, night black, forest
green, notebook white). The drawn person wears a magenta shirt, the mark the fake face
detector looks for.
"""

from __future__ import annotations

import math
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw

from engine.library.transcribe import Transcript, Word

SIZE = (640, 360)
STILL_KINDS = ("sunrise", "ocean", "night", "forest", "notebook")
# The done-when's five sample beats (P0.6b.1): what a scriptwriter asks for a cutaway.
SAMPLE_BEATS = [
    ("sunrise", "the sun rising over the horizon at dawn"),
    ("ocean", "waves rolling on a blue ocean"),
    ("night", "a starry night sky with the moon"),
    ("forest", "a green forest of pine trees"),
    ("notebook", "an open notebook with lined paper"),
]


def _sunrise(d: ImageDraw.ImageDraw, w: int, h: int) -> None:
    horizon = int(h * 0.62)
    for y in range(horizon):
        t = y / horizon
        d.line([(0, y), (w, y)], fill=(255, int(120 + 70 * t), int(20 + 50 * t)))
    d.ellipse([w / 2 - 70, horizon - 70, w / 2 + 70, horizon + 70], fill=(255, 225, 110))
    d.rectangle([0, horizon, w, h], fill=(230, 120, 30))
    for i, y in enumerate(range(horizon + 10, h, 14)):
        half = 60 - i * 3
        d.line([(w / 2 - half, y), (w / 2 + half, y)], fill=(255, 200, 90), width=3)


def _ocean(d: ImageDraw.ImageDraw, w: int, h: int) -> None:
    d.rectangle([0, 0, w, h * 0.3], fill=(90, 150, 230))
    d.rectangle([0, h * 0.3, w, h], fill=(20, 80, 180))
    for row, y0 in enumerate(range(int(h * 0.36), h, 28)):
        points = [(x, y0 + 7 * math.sin(x / 38 + row)) for x in range(0, w + 8, 8)]
        d.line(points, fill=(235, 245, 255), width=4)


def _night(d: ImageDraw.ImageDraw, w: int, h: int) -> None:
    d.rectangle([0, 0, w, h], fill=(8, 12, 36))
    for i in range(140):
        x, y = (i * 7919) % w, (i * 104729) % h
        r = 1 + i % 3
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 240))
    d.ellipse([w * 0.7, h * 0.12, w * 0.7 + 90, h * 0.12 + 90], fill=(250, 245, 200))
    d.ellipse([w * 0.7 + 28, h * 0.12 - 8, w * 0.7 + 118, h * 0.12 + 82], fill=(8, 12, 36))


def _forest(d: ImageDraw.ImageDraw, w: int, h: int) -> None:
    d.rectangle([0, 0, w, h], fill=(210, 230, 210))
    d.rectangle([0, h * 0.7, w, h], fill=(40, 150, 60))
    for i, x in enumerate(range(-20, w + 40, 55)):
        top = h * (0.12 + 0.08 * (i % 3))
        d.rectangle([x - 6, h * 0.65, x + 6, h * 0.8], fill=(90, 60, 30))
        d.polygon([(x, top), (x - 40, h * 0.72), (x + 40, h * 0.72)], fill=(20, 100, 35))


def _notebook(d: ImageDraw.ImageDraw, w: int, h: int) -> None:
    d.rectangle([0, 0, w, h], fill=(60, 60, 62))
    d.rectangle([w * 0.06, h * 0.06, w * 0.94, h * 0.94], fill=(248, 246, 236))
    d.line([(w / 2, h * 0.06), (w / 2, h * 0.94)], fill=(150, 150, 150), width=4)
    for y in range(int(h * 0.14), int(h * 0.92), 18):
        d.line([(w * 0.08, y), (w * 0.48, y)], fill=(150, 180, 230), width=1)
        d.line([(w * 0.52, y), (w * 0.92, y)], fill=(150, 180, 230), width=1)
    d.line([(w * 0.14, h * 0.08), (w * 0.14, h * 0.92)], fill=(230, 120, 120), width=2)
    for i, y in enumerate(range(int(h * 0.14) - 4, int(h * 0.5), 18)):
        d.line([(w * 0.16, y), (w * (0.42 - 0.03 * (i % 3)), y)], fill=(40, 40, 90), width=2)


def _person(d: ImageDraw.ImageDraw, w: int, h: int) -> None:
    """Head and shoulders, front on, in a magenta shirt (the fake detector's mark)."""
    d.rectangle([0, 0, w, h], fill=(200, 200, 205))
    cx, cy = w / 2, h * 0.42
    d.rounded_rectangle([cx - 130, h * 0.72, cx + 130, h + 20], radius=60, fill=(255, 0, 255))
    d.rectangle([cx - 22, cy + 70, cx + 22, h * 0.75], fill=(224, 172, 140))
    d.ellipse([cx - 72, cy - 105, cx + 72, cy - 10], fill=(50, 35, 25))  # hair
    d.ellipse([cx - 62, cy - 80, cx + 62, cy + 85], fill=(232, 185, 150))  # face
    for side in (-1, 1):
        ex = cx + side * 26
        d.line([(ex - 16, cy - 30), (ex + 16, cy - 32)], fill=(60, 40, 30), width=4)
        d.ellipse([ex - 13, cy - 18, ex + 13, cy - 2], fill=(255, 255, 255))
        d.ellipse([ex - 6, cy - 16, ex + 6, cy - 4], fill=(40, 30, 25))
    d.polygon([(cx, cy - 5), (cx - 9, cy + 25), (cx + 9, cy + 25)], fill=(205, 150, 120))
    d.arc([cx - 26, cy + 30, cx + 26, cy + 58], 20, 160, fill=(150, 60, 60), width=4)


DRAW = {"sunrise": _sunrise, "ocean": _ocean, "night": _night, "forest": _forest,
        "notebook": _notebook, "person": _person}  # fmt: skip


def draw_still(kind: str, size: tuple[int, int] = SIZE) -> Image.Image:
    image = Image.new("RGB", size)
    DRAW[kind](ImageDraw.Draw(image), *size)
    return image


def make_clip(path: Path, stills: Sequence[str], seconds_each: float = 2.0,
              tone: bool = False) -> Path:  # fmt: skip
    """An MP4 of the named stills, hard cut, 24 fps; `tone` adds a sine audio track."""
    with tempfile.TemporaryDirectory() as tmp:
        inputs, labels = [], []
        for n, kind in enumerate(stills):
            png = Path(tmp) / f"{n}.png"
            draw_still(kind).save(png)
            inputs += ["-loop", "1", "-framerate", "24", "-t", f"{seconds_each}", "-i", str(png)]
            labels.append(f"[{n}:v]")
        graph = f"{''.join(labels)}concat=n={len(stills)}:v=1:a=0,format=yuv420p[v]"
        audio = []
        if tone:
            total = seconds_each * len(stills)
            audio = ["-f", "lavfi", "-i", f"sine=frequency=330:duration={total}"]
        argv = ["ffmpeg", "-v", "error", "-y", *inputs, *audio, "-filter_complex", graph,
                "-map", "[v]", *(["-map", f"{len(stills)}:a", "-c:a", "aac"] if tone else []),
                "-c:v", "libx264", "-preset", "ultrafast", "-r", "24", str(path)]  # fmt: skip
        subprocess.run(argv, check=True)
    return path


# A whole fixture library, ingested ------------------------------------------------------------

TODAY = date(2026, 11, 20)
SPOKEN = "breathe in slowly and let the waves carry you"


class SpokenWhisper:
    """Stands in for Whisper: every clip with sound says SPOKEN."""

    def transcribe(self, audio: Path) -> Transcript:
        words = SPOKEN.split()
        return Transcript("en", "fake", [Word(w, 0.2 * n, 0.2 * n + 0.18)
                                         for n, w in enumerate(words)])  # fmt: skip


def build_library(folder: Path, deps, media: Path):
    """Five clean scenery clips (one with speech), a clip with a person, an expired stock
    clip and a film excerpt; ingested by the real Ingester. Returns it and asset ids by name."""
    from engine.config import load_config
    from engine.config.models import SourceConfig
    from engine.db import open_db
    from engine.library.ingest import Ingester
    from engine.library.understand import detect_scenes, embed_keyframes
    from engine.settings import REPO_ROOT

    for sub in ("own", "stock", "film"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    for kind in STILL_KINDS:
        make_clip(folder / "own" / f"{kind}.mp4", [kind], tone=kind == "ocean")
    make_clip(folder / "own" / "friend.mp4", ["sunrise", "person"])
    make_clip(folder / "stock" / "old-forest.mp4", ["forest"])
    film = make_clip(folder / "film" / "Sea Film (1999).mp4", ["ocean"])
    sources = [
        SourceConfig(id="own-clips", kind="folder", path=str(folder / "own"),
                     rights_type="own", rights_proof="Drawn for the tests"),
        SourceConfig(id="stock-clips", kind="folder", path=str(folder / "stock"),
                     rights_type="stock", rights_proof="Pexels licence",
                     licence_url="https://www.pexels.com/license/", attribution="Pexels",
                     expires_at=date(2026, 1, 1)),
        SourceConfig(id="films", kind="drive", rights_type="film_reference",
                     rights_proof="own collection", volume_serial="1", volume_label="F"),
    ]  # fmt: skip
    config = replace(load_config(REPO_ROOT / "config"), sources=sources)
    ingester = Ingester(open_db(folder / "engine.db"), config, deps, media)
    ingester.run()
    conn = ingester.conn
    film_id = conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, duration, rights_type, "
        "people_release) VALUES ('films', ?, ?, 'video', 2.0, 'film_reference', 'none')",
        ("9" * 64, str(film)),
    ).lastrowid
    detect_scenes(conn, film_id, film, 2.0, media / "index" / "films" / str(film_id), deps.run)
    embed_keyframes(conn, film_id, deps.clip)
    ids = {Path(r["path"]).stem: r["id"] for r in conn.execute("SELECT id, path FROM assets")}
    return ingester, ids


# Music pool fixtures (FR-ING-6, P0.6b.2) ---------------------------------------------------------

TRACK_RATE = 44100


def make_track(path: Path, bpm: float, seconds: float = 20.0, title: str | None = None,
               artist: str | None = None) -> Path:  # fmt: skip
    """A click track at `bpm` (a 30 ms 1 kHz burst on every beat), encoded by the suffix, with
    title and artist tags when given. Generated, never a real song (NFR-9)."""
    import numpy as np

    t = np.arange(int(TRACK_RATE * seconds)) / TRACK_RATE
    since_beat = np.mod(t, 60.0 / bpm)
    click = np.sin(2 * np.pi * 1000 * t) * np.exp(-since_beat * 120) * (since_beat < 0.03)
    pcm = (click * 0.8 * 32767).astype("<i2").tobytes()
    tags = [*(["-metadata", f"title={title}"] if title else []),
            *(["-metadata", f"artist={artist}"] if artist else [])]  # fmt: skip
    codec = {".mp3": ["-c:a", "libmp3lame", "-b:a", "128k"], ".m4a": ["-c:a", "aac"],
             ".wav": ["-c:a", "pcm_s16le"]}[path.suffix]  # fmt: skip
    argv = ["ffmpeg", "-v", "error", "-y", "-f", "s16le", "-ar", str(TRACK_RATE), "-ac", "1",
            "-i", "-", *tags, *codec, str(path)]  # fmt: skip
    subprocess.run(argv, input=pcm, check=True)
    return path
