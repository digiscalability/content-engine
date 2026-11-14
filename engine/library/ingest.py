"""Own-footage ingest (SRD FR-ING-1, FR-ING-4, FR-SRC-2; FR-UND-1..3, FR-UND-5). ADR 0022.

Every folder source in the sources config that is not a film drive is scanned. Files are
indexed in place and read-only: nothing is written beside the footage and nothing is copied.
Each video is hashed (SHA-256) and deduped: the same bytes under two paths are one asset, and
a path whose size and mtime have not changed since it was hashed is not read again.

Per asset, in order, each step committed on its own with its `asset_index` time:
1. probe: duration, picture size, whether there is sound (ffprobe);
2. transcript: Whisper under the GPU lock, only for clips with sound;
3. scenes: scene cuts and one keyframe each, under `media/index/own/<asset_id>/`;
4. chunks: transcript chunks with text vectors;
5. images: a CLIP vector per keyframe;
6. people: a frame every second and every keyframe through the face detector. No face: the
   asset's `people_release` becomes `none`. A face: it stays `unknown`, off B-roll search,
   until Abbas marks it (§8.3). A mark is never overwritten.

A step that fails for any reason (a broken file, a model error) is recorded and the asset is
skipped until `--retry-failed`; the run goes on with the next file. Each scan copies the
source's licence fields onto its assets again, so a renewed or changed licence reaches clips
already ingested. Ingest stops below the disk stop line (NFR-5).
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from engine.config import Config
from engine.config.models import SourceConfig
from engine.library.assets import file_sha256
from engine.library.clip import ImageTextEmbedder
from engine.library.embeddings import Embedder
from engine.library.faces import FaceDetector
from engine.library.film_index import VIDEO_SUFFIXES, discover
from engine.library.sources import sync_sources
from engine.library.transcribe import Transcriber, Word
from engine.library.understand import (
    StepError,
    count_faces,
    detect_scenes,
    embed_keyframes,
    store_chunks,
)
from engine.ops.budget import disk_preflight
from engine.ops.gpu_lock import GpuLock
from engine.produce.ffmpeg import probe
from engine.produce.process import Runner, output_tail, run_command
from engine.settings import REPO_ROOT

OWN_SUFFIXES = frozenset(VIDEO_SUFFIXES | {".3gp", ".mts", ".m2ts"})
ALL_STEPS = ("probe", "transcript", "scenes", "chunks", "images", "people")
COLUMNS = {"probe": "probed_at", "transcript": "transcript_at", "scenes": "scenes_at",
           "chunks": "chunks_at", "images": "images_at", "people": "people_at"}  # fmt: skip
NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


@dataclass
class IngestDeps:
    embedder: Embedder
    clip: ImageTextEmbedder
    faces: FaceDetector
    transcriber: Callable[[], Transcriber]  # built on first use: Whisper loads only if needed
    run: Runner = run_command
    gpu_lock: Path | None = None
    disk_free: Callable[[Path], int] = lambda path: 1 << 50
    hasher: Callable[[Path], str] = file_sha256


@dataclass
class AssetResult:
    path: str
    asset_id: int | None = None
    done: list[str] = field(default_factory=list)
    error: str | None = None
    skipped: str | None = None


@dataclass
class IngestReport:
    assets: list[AssetResult] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    stopped: str | None = None


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def source_root(source: SourceConfig) -> Path:
    path = Path(os.path.expandvars(source.path or ""))
    return path if path.is_absolute() else REPO_ROOT / path


def stock_folder(media_root: Path, source_id: str) -> Path:
    """Where `ce stock fetch` puts a stock API's clips (FR-ING-5, ADR 0023)."""
    return media_root / "library" / "stock" / source_id


def is_stock_api(source: SourceConfig) -> bool:
    return source.kind == "library" and bool(source.url)


def ingestible(config: Config) -> list[SourceConfig]:
    """Folder sources other than film drives (§8.1), and stock APIs, whose fetched clips sit
    in the media folder. Film drives go through `ce index-films`; music pools are imported by
    `engine/library/music.py`."""
    return [
        s for s in config.sources
        if (s.kind == "folder" and s.rights_type != "film_reference") or is_stock_api(s)
    ]  # fmt: skip


def ingest_root(source: SourceConfig, media_root: Path) -> Path:
    return stock_folder(media_root, source.id) if is_stock_api(source) else source_root(source)


def register_file(conn: sqlite3.Connection, source: SourceConfig, path: Path, sha: str,
                  media_kind: str, people_release: str | None = None) -> int:  # fmt: skip
    """The asset for these bytes (made if new, with the source's tier and licence) and this
    path's row with its size and mtime. Same bytes under two paths are one asset. The caller
    holds the transaction."""
    stat = path.stat()
    key = str(path.resolve())
    expires = source.expires_at.isoformat() if source.expires_at else None
    conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, rights_type, licence_url, "
        "attribution, licence_expires_at, people_release) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, coalesce(?, 'unknown')) ON CONFLICT(sha256) DO NOTHING",
        (source.id, sha, key, media_kind, source.rights_type, source.licence_url,
         source.attribution, expires, people_release),
    )  # fmt: skip
    asset_id = int(conn.execute("SELECT id FROM assets WHERE sha256 = ?", (sha,)).fetchone()[0])
    conn.execute(
        "INSERT INTO asset_paths (path, asset_id, size, mtime_ns) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET asset_id = excluded.asset_id, "
        "size = excluded.size, mtime_ns = excluded.mtime_ns",
        (key, asset_id, stat.st_size, stat.st_mtime_ns),
    )
    return asset_id


def known_path(conn: sqlite3.Connection, path: Path) -> int | None:
    """The asset of a path whose size and mtime have not changed since it was hashed."""
    stat = path.stat()
    row = conn.execute(
        "SELECT asset_id FROM asset_paths WHERE path = ? AND size = ? AND mtime_ns = ?",
        (str(path.resolve()), stat.st_size, stat.st_mtime_ns),
    ).fetchone()
    return int(row[0]) if row else None


class Ingester:
    def __init__(self, conn: sqlite3.Connection, config: Config, deps: IngestDeps,
                 media_root: Path) -> None:  # fmt: skip
        self.conn, self.config, self.deps, self.media_root = conn, config, deps, media_root
        self.keyframes = media_root / "index" / "own"
        self.work = media_root / "work" / "ingest"

    def run(self, source_id: str | None = None, retry_failed: bool = False) -> IngestReport:
        sources = ingestible(self.config)
        if source_id is not None:
            sources = [s for s in sources if s.id == source_id]
            if not sources:
                names = ", ".join(s.id for s in ingestible(self.config)) or "none"
                raise ValueError(
                    f"'{source_id}' is not an ingestible folder source (they are: {names})"
                )
        report = IngestReport()
        sync_sources(self.conn, self.config)
        for source in sources:
            self._refresh_licence(source)
            root = ingest_root(source, self.media_root)
            if not root.is_dir():
                if not is_stock_api(source):  # a stock folder appears with the first fetch
                    report.missing.append(source.id)
                continue
            for path in discover(root, OWN_SUFFIXES):
                if reason := self._stop_reason():
                    report.stopped = reason
                    return report
                report.assets.append(self._ingest_one(source, path, retry_failed))
        return report

    def _refresh_licence(self, source: SourceConfig) -> None:
        """FR-SRC-2: the source's licence URL, attribution and expiry on all its assets."""
        expires = source.expires_at.isoformat() if source.expires_at else None
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE assets SET licence_url = ?, attribution = ?, licence_expires_at = ? "
                "WHERE source_id = ?",
                (source.licence_url, source.attribution, expires, source.id),
            )

    def _stop_reason(self) -> str | None:
        free = self.deps.disk_free(self.media_root)
        return disk_preflight(free, self.config.engine.disk.stop_free_gb)

    # registering -------------------------------------------------------------------------

    def register(self, source: SourceConfig, path: Path) -> int:
        """The asset for this file: known path with the same size and mtime, else by hash."""
        known = known_path(self.conn, path)
        if known is not None:
            return known
        sha = self.deps.hasher(path)
        with transaction(self.conn):
            asset_id = register_file(self.conn, source, path, sha, "video")
            self.conn.execute("INSERT OR IGNORE INTO asset_index (asset_id) VALUES (?)",
                              (asset_id,))  # fmt: skip
        return asset_id

    # the steps ---------------------------------------------------------------------------

    def _asset(self, asset_id: int) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT a.*, i.has_audio FROM assets a JOIN asset_index i ON i.asset_id = a.id "
            "WHERE a.id = ?",
            (asset_id,),
        ).fetchone()

    def _probe(self, asset_id: int, path: Path) -> None:
        try:
            info = probe(path, self.deps.run)
        except Exception as exc:  # ffprobe on a broken or partial file
            raise StepError(f"probe failed: {exc}") from exc
        self.conn.execute(
            "UPDATE assets SET duration = ?, width = ?, height = ?, fps = ? WHERE id = ?",
            (info.duration, info.width, info.height, info.fps, asset_id),
        )
        self.conn.execute(
            "UPDATE asset_index SET has_audio = ? WHERE asset_id = ?",
            (int(info.audio_codec is not None), asset_id),
        )

    def _transcript(self, asset_id: int, path: Path) -> None:
        if not self._asset(asset_id)["has_audio"]:
            return  # no sound, nothing to hear
        self.work.mkdir(parents=True, exist_ok=True)
        wav = self.work / f"asset-{asset_id}.wav"
        try:
            argv = ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vn", "-ac", "1",
                    "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]  # fmt: skip
            result = self.deps.run(argv, None)
            if result.returncode != 0:
                raise StepError(f"audio extraction failed: {output_tail(result)}")
            lock = (GpuLock(self.deps.gpu_lock, job=f"ingest asset {asset_id}")
                    if self.deps.gpu_lock else nullcontext())  # fmt: skip
            with lock:  # NFR-6: one GPU job at a time
                heard = self.deps.transcriber().transcribe(wav)
        finally:
            wav.unlink(missing_ok=True)
        timed = [{"text": w.text, "start": w.start, "end": w.end} for w in heard.words]
        self.conn.execute(
            "INSERT OR REPLACE INTO transcripts (asset_id, model, language, words) "
            "VALUES (?, ?, ?, ?)",
            (asset_id, f"whisper:{heard.model}", heard.language, json.dumps(timed)),
        )

    def _scenes(self, asset_id: int, path: Path) -> None:
        duration = self._asset(asset_id)["duration"]
        detect_scenes(self.conn, asset_id, path, duration, self.keyframes / str(asset_id),
                      self.deps.run)  # fmt: skip

    def _chunks(self, asset_id: int, path: Path) -> None:
        row = self.conn.execute(
            "SELECT words FROM transcripts WHERE asset_id = ? ORDER BY created_at DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
        words = [Word(w["text"], w["start"], w["end"]) for w in json.loads(row[0])] if row else []
        store_chunks(self.conn, asset_id, words, self.deps.embedder)

    def _images(self, asset_id: int, path: Path) -> None:
        embed_keyframes(self.conn, asset_id, self.deps.clip)

    def _people(self, asset_id: int, path: Path) -> None:
        keyframes = [
            (Path(r["keyframe_path"]), (r["start_s"] + r["end_s"]) / 2)
            for r in self.conn.execute(
                "SELECT keyframe_path, start_s, end_s FROM scenes "
                "WHERE asset_id = ? AND keyframe_path IS NOT NULL",
                (asset_id,),
            )
        ]
        frames, first = count_faces(path, keyframes, self.deps.faces, self.deps.run,
                                    self.work / f"faces-{asset_id}")  # fmt: skip
        self.conn.execute(
            "UPDATE asset_index SET faces = ?, face_first_s = ? WHERE asset_id = ?",
            (frames, first, asset_id),
        )
        if frames == 0:  # §8.3: only an unmarked, unchecked asset becomes 'none'
            self.conn.execute(
                "UPDATE assets SET people_release = 'none' WHERE id = ? "
                "AND people_release = 'unknown' "
                "AND NOT EXISTS (SELECT 1 FROM people_marks WHERE asset_id = ?)",
                (asset_id, asset_id),
            )

    # one asset ---------------------------------------------------------------------------

    def _ingest_one(self, source: SourceConfig, path: Path, retry_failed: bool) -> AssetResult:
        result = AssetResult(str(path))
        try:
            asset_id = self.register(source, path)
        except OSError as exc:
            result.error = f"unreadable: {exc}"
            return result
        result.asset_id = asset_id
        status = self.conn.execute(
            "SELECT * FROM asset_index WHERE asset_id = ?", (asset_id,)
        ).fetchone()
        if status["error"] and not retry_failed:
            result.skipped = f"failed before at {status['error_step']}: {status['error']}"
            return result
        steps = {"probe": self._probe, "transcript": self._transcript, "scenes": self._scenes,
                 "chunks": self._chunks, "images": self._images,
                 "people": self._people}  # fmt: skip
        for step in ALL_STEPS:
            if status[COLUMNS[step]]:
                continue
            if reason := self._stop_reason():
                result.skipped = reason
                return result
            try:
                with transaction(self.conn):
                    steps[step](asset_id, path)
                    self.conn.execute(
                        f"UPDATE asset_index SET {COLUMNS[step]} = {NOW}, error = NULL, "
                        f"error_step = NULL, updated_at = {NOW} WHERE asset_id = ?",
                        (asset_id,),
                    )
            except Exception as exc:  # recorded, never fatal: the night goes on with the next file
                self.conn.execute(
                    f"UPDATE asset_index SET error_step = ?, error = ?, updated_at = {NOW} "
                    "WHERE asset_id = ?",
                    (step, str(exc)[:500], asset_id),
                )
                result.error = f"{step}: {exc}"
                return result
            result.done.append(step)
        return result
