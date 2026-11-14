"""Understanding steps shared by the own-footage ingest and the film index (SRD FR-UND-2,
FR-UND-3, FR-UND-5; ADR 0020, ADR 0022).

- scenes: ffmpeg's scene score on a 320 px proxy (the style analyser's method), scenes of at
  least a second, one 320 px JPEG keyframe per scene at its midpoint;
- chunks: about 30 s of transcript per chunk, each with a text vector;
- keyframe vectors: CLIP over every keyframe;
- faces: a 640 px frame every second plus every keyframe, counted by a face detector.

Each step rewrites its rows for one asset and drops that asset's old vectors first, so a
re-run never leaves a vector pointing at a deleted or reused row. Callers commit.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image

from engine.library.clip import ImageTextEmbedder
from engine.library.embeddings import Embedder
from engine.library.faces import FaceDetector
from engine.library.transcribe import Word
from engine.library.vectors import (
    drop_image_vectors,
    drop_text_vectors,
    store_image,
    store_text,
)
from engine.produce.process import Runner, output_tail
from engine.style.measure import SCENE_THRESHOLD, parse_scene_cuts

MIN_SCENE_S = 1.0
CHUNK_S = 30.0
FACE_SAMPLE_S = 1.0
FACE_FRAME_WIDTH = 640


class StepError(RuntimeError):
    """One step of one asset failed; the asset is recorded and skipped."""


# Scenes and keyframes (FR-UND-2) -----------------------------------------------------------


def scene_command(video: Path) -> list[str]:
    graph = f"scale=320:-2,select='gt(scene,{SCENE_THRESHOLD})',showinfo"
    return ["ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
            "-vf", graph, "-an", "-sn", "-f", "null", "-"]  # fmt: skip


def scenes_from_cuts(cuts: Sequence[float], duration: float) -> list[tuple[float, float]]:
    """Scene ranges from cut times; cuts closer than a second merge into one scene."""
    bounds = [0.0]
    for cut in sorted(cuts):
        if MIN_SCENE_S <= cut <= duration - MIN_SCENE_S and cut - bounds[-1] >= MIN_SCENE_S:
            bounds.append(round(cut, 3))
    bounds.append(round(duration, 3))
    return list(zip(bounds, bounds[1:], strict=False))


def keyframe_command(video: Path, at: float, out: Path) -> list[str]:
    return ["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", str(video),
            "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4", str(out)]  # fmt: skip


def _ffmpeg(argv: list[str], run: Runner, what: str) -> None:
    result = run(argv, None)
    if result.returncode != 0:
        raise StepError(f"{what} failed: {output_tail(result)}")


def detect_scenes(conn: sqlite3.Connection, asset_id: int, video: Path, duration: float,
                  folder: Path, run: Runner) -> int:  # fmt: skip
    """Rewrite the asset's scenes with one keyframe each under `folder`; the count."""
    result = run(scene_command(video), None)
    if result.returncode != 0:
        raise StepError(f"scene detection failed: {output_tail(result)}")
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for n, (start, end) in enumerate(scenes_from_cuts(parse_scene_cuts(result.stderr),
                                                      duration), start=1):  # fmt: skip
        out = folder / f"scene-{n:05d}.jpg"
        _ffmpeg(keyframe_command(video, (start + end) / 2, out), run, "keyframe")
        rows.append((asset_id, start, end, str(out)))
    drop_image_vectors(conn, asset_id)
    conn.execute("DELETE FROM scenes WHERE asset_id = ?", (asset_id,))
    conn.executemany(
        "INSERT INTO scenes (asset_id, start_s, end_s, keyframe_path) VALUES (?, ?, ?, ?)", rows
    )
    return len(rows)


# Transcript chunks and keyframe vectors (FR-UND-3) -----------------------------------------


def chunk_words(words: Sequence[Word], span: float = CHUNK_S) -> list[tuple[float, float, str]]:
    """Consecutive words or cues grouped into chunks of about `span` seconds."""
    chunks, current = [], []
    for word in words:
        current.append(word)
        if current[-1].end - current[0].start >= span:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return [
        (c[0].start, max(c[-1].end, c[0].start + 0.001), " ".join(w.text for w in c))
        for c in chunks
    ]


def store_chunks(conn: sqlite3.Connection, asset_id: int, words: Sequence[Word],
                 embedder: Embedder) -> int:  # fmt: skip
    """Rewrite the asset's transcript chunks and their text vectors; the count."""
    chunks = chunk_words(words)
    vectors = embedder.embed([c[2] for c in chunks]) if chunks else []
    drop_text_vectors(conn, asset_id)
    conn.execute("DELETE FROM transcript_chunks WHERE asset_id = ?", (asset_id,))
    for (start, end, text), vector in zip(chunks, vectors, strict=True):
        chunk_id = conn.execute(
            "INSERT INTO transcript_chunks (asset_id, start_s, end_s, text, model, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (asset_id, start, end, text, embedder.name, np.asarray(vector, "<f4").tobytes()),
        ).lastrowid
        store_text(conn, chunk_id, vector)
    return len(chunks)


def embed_keyframes(conn: sqlite3.Connection, asset_id: int, clip: ImageTextEmbedder) -> int:
    """A CLIP vector for every keyframe of the asset; the count."""
    rows = conn.execute(
        "SELECT id, keyframe_path FROM scenes WHERE asset_id = ? AND keyframe_path IS NOT NULL "
        "ORDER BY start_s",
        (asset_id,),
    ).fetchall()
    if not rows:
        return 0
    vectors = clip.embed_images([Path(r["keyframe_path"]) for r in rows])
    for row, vector in zip(rows, vectors, strict=True):
        store_image(conn, row["id"], vector, clip.name)
    return len(rows)


# Faces (FR-UND-5) ---------------------------------------------------------------------------


def _bgr(path: Path) -> np.ndarray:
    with Image.open(path) as image:  # PIL reads any Windows path; cv2.imread does not
        return np.asarray(image.convert("RGB"))[:, :, ::-1].copy()


def count_faces(video: Path, keyframes: Sequence[tuple[Path, float]], detector: FaceDetector,
                run: Runner, work: Path, every_s: float = FACE_SAMPLE_S
                ) -> tuple[int, float | None]:  # fmt: skip
    """Frames showing a face, and the time of the first: a frame every `every_s` seconds
    from the start, plus each keyframe at its time. Sampled frames and `work` are deleted
    after. A check that examined no frame at all raises: it must not pass as 'no faces'."""
    if work.exists():  # frames left by a killed run would be counted as samples
        shutil.rmtree(work)
    work.mkdir(parents=True)
    pattern = work / "face-%05d.png"
    graph = (f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{every_s})',"
             f"scale={FACE_FRAME_WIDTH}:-2,showinfo")  # fmt: skip
    argv = ["ffmpeg", "-hide_banner", "-nostats", "-y", "-i", str(video), "-an", "-sn",
            "-vf", graph, "-fps_mode", "vfr", str(pattern)]  # fmt: skip
    try:
        result = run(argv, None)
        if result.returncode != 0:
            raise StepError(f"face sampling failed: {output_tail(result)}")
        times = parse_scene_cuts(result.stderr)
        frames = sorted(work.glob("face-*.png"))
        samples = [(path, times[n] if n < len(times) else None) for n, path in enumerate(frames)]
        examined = [*samples, *keyframes]
        if not examined:
            raise StepError("the face check saw no frame of this video; it stays held back")
        seen = [at for path, at in examined if detector.count(_bgr(path)) > 0]
    finally:
        shutil.rmtree(work, ignore_errors=True)
    timed = [at for at in seen if at is not None]
    return len(seen), (round(min(timed), 3) if timed else None)
