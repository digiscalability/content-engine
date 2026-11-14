"""The film drive indexer (SRD §8.2, FR-UND-1..3): every film on a registered drive, indexed
in place and read-only, resumable. ADR 0020.

Per film, in order, each step committed on its own:
1. probe: duration and picture size (ffprobe);
2. transcript: a sidecar .srt/.vtt, else an embedded text subtitle stream, else Whisper
   large-v3 on 16 kHz audio under the GPU lock (one film at a time, so the 04:00 render can
   take its turn);
3. scenes: ffmpeg scene cuts on a 320 px proxy, scenes of at least a second, one 320 px JPEG
   keyframe per scene under `media/index/films/<asset_id>/`;
4. chunks: about 30 s of transcript per chunk, embedded with the text model;
5. images: a CLIP vector per keyframe (ADR 0022). With no CLIP model the step waits, so a
   film indexed without it gets only this step on a later run.

Scenes, chunks and keyframe vectors are the shared steps of `engine.library.understand`.

Nothing is written to the drive and no film is copied: only the DB, keyframes and temporary
audio or subtitle files under `media/work/`, deleted after use. A film is known by a sampled
fingerprint (its size and three 4 MiB samples), so a moved or renamed file is the same asset
and nothing is read in full just to recognise it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from engine.config import Config
from engine.config.models import SourceConfig
from engine.library.clip import ImageTextEmbedder
from engine.library.embeddings import Embedder
from engine.library.sources import sync_sources
from engine.library.subtitles import parse_subtitles
from engine.library.transcribe import Transcriber, Word
from engine.library.understand import (  # noqa: F401  (re-exported for callers)
    StepError,
    chunk_words,
    detect_scenes,
    embed_keyframes,
    keyframe_command,
    scene_command,
    scenes_from_cuts,
    store_chunks,
)
from engine.library.volumes import Volume, find_volume
from engine.ops.budget import disk_preflight
from engine.ops.gpu_lock import GpuLock
from engine.produce.ffmpeg import probe
from engine.produce.process import Runner, output_tail, run_command

VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".ts", ".webm"}
SKIP_DIRS = {"$recycle.bin", "system volume information", "recycler"}
SAMPLE_BYTES = 4 * 1024 * 1024
TEXT_SUBTITLES = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
STEPS = ("probe", "transcript", "scenes", "chunks", "images")


FilmIndexError = StepError  # one step of one film failed; the film is recorded and skipped


# Discovery -------------------------------------------------------------------------------


def discover(root: Path, suffixes: frozenset[str] | set[str] = VIDEO_SUFFIXES) -> list[Path]:
    """Video files under `root`, skipping system and hidden folders, in a stable order."""
    found = []
    if not root.is_dir():
        return found
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if any(p.lower() in SKIP_DIRS or p.startswith(".") for p in parts[:-1]):
            continue
        if path.is_file() and path.suffix.lower() in suffixes:
            found.append(path)
    return found


def film_fingerprint(path: Path) -> str:
    """sha256 of the size and three 4 MiB samples (start, middle, end)."""
    size = path.stat().st_size
    digest = hashlib.sha256(f"film-v1:{size}:".encode())
    offsets = sorted({0, max(0, size // 2 - SAMPLE_BYTES // 2), max(0, size - SAMPLE_BYTES)})
    with path.open("rb") as handle:  # read-only
        for offset in offsets:
            handle.seek(offset)
            digest.update(handle.read(SAMPLE_BYTES))
    return digest.hexdigest()


_YEAR = re.compile(r"[\s._(\[-]\(?((?:19|20)\d{2})\)?(?=[\s._)\]-]|$)")


def film_title(path: Path) -> str:
    """'Heat.1995.1080p.BluRay' -> 'Heat (1995)'; the on-screen credit starts here (§8.2).
    The year is the first one after some title, so '2001.A.Space.Odyssey.1968' keeps 2001."""
    stem = path.stem
    for match in _YEAR.finditer(stem):
        title = re.sub(r"[._]+", " ", stem[: match.start()]).strip(" -([")
        if title:
            return f"{title} ({match.group(1)})"
    return re.sub(r"[._]+", " ", stem).strip()


# Transcript --------------------------------------------------------------------------------


def find_sidecar(film: Path) -> Path | None:
    """A subtitle file beside the film with the same stem; English ones first."""
    candidates = [
        p for p in film.parent.iterdir()
        if p.suffix.lower() in (".srt", ".vtt") and p.name.lower().startswith(film.stem.lower())
    ]  # fmt: skip

    def rank(p: Path) -> tuple[int, str]:
        tags = p.name.lower()[len(film.stem) :]
        return (0 if re.search(r"\.(en|eng|english)\b", tags) else 1, p.name)

    return min(candidates, key=rank) if candidates else None


def text_subtitle_stream(film: Path, run: Runner) -> int | None:
    """The index of the film's English text subtitle stream, else its first text one."""
    argv = ["ffprobe", "-v", "error", "-select_streams", "s",
            "-show_entries", "stream=index,codec_name:stream_tags=language", "-of", "json",
            str(film)]  # fmt: skip
    result = run(argv, None)
    if result.returncode != 0:
        return None
    streams = [
        s for s in json.loads(result.stdout or "{}").get("streams", [])
        if s.get("codec_name") in TEXT_SUBTITLES
    ]  # fmt: skip
    english = [s for s in streams if s.get("tags", {}).get("language") in ("eng", "en")]
    chosen = (english or streams or [None])[0]
    return None if chosen is None else int(chosen["index"])


def _ffmpeg(argv: list[str], run: Runner, what: str) -> None:
    result = run(argv, None)
    if result.returncode != 0:
        raise FilmIndexError(f"{what} failed: {output_tail(result)}")


# The run -----------------------------------------------------------------------------------

MAX_WINDOW_H = 8  # §13: the 00:00-06:00 window; a run caught up later in the day does nothing


def next_deadline(hhmm: str, now: datetime, max_hours: float = MAX_WINDOW_H) -> datetime | None:
    """The next `hhmm` after `now`, or None when it is more than `max_hours` away: Task
    Scheduler's catch-up may start a missed 00:00 run at 07:00, and that run should not index
    all day."""
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", hhmm.strip())
    if match is None:
        raise ValueError(f"--until takes HH:MM (24 h), not {hhmm!r}")
    deadline = now.replace(hour=int(match[1]), minute=int(match[2]), second=0, microsecond=0)
    if deadline <= now:
        deadline += timedelta(days=1)
    return deadline if deadline - now <= timedelta(hours=max_hours) else None


@dataclass
class IndexDeps:
    embedder: Embedder
    transcriber: Callable[[], Transcriber]  # built on first use: Whisper loads only if needed
    clip: ImageTextEmbedder | None = None  # None: the images step waits for a later run
    run: Runner = run_command
    gpu_lock: Path | None = None
    disk_free: Callable[[Path], int] = lambda path: 1 << 50
    clock: Callable[[], datetime] = datetime.now


@dataclass
class FilmResult:
    title: str
    asset_id: int | None
    done: list[str] = field(default_factory=list)
    error: str | None = None
    skipped: str | None = None


@dataclass
class IndexReport:
    films: list[FilmResult] = field(default_factory=list)
    missing_drives: list[str] = field(default_factory=list)
    stopped: str | None = None


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


class FilmIndexer:
    def __init__(self, conn: sqlite3.Connection, config: Config, deps: IndexDeps,
                 media_root: Path) -> None:  # fmt: skip
        self.conn, self.config, self.deps, self.media_root = conn, config, deps, media_root
        self.work = media_root / "work" / "film-index"
        self.keyframes = media_root / "index" / "films"

    # one film ----------------------------------------------------------------------------

    def register(self, source: SourceConfig, volume: Volume, film: Path) -> int:
        sha = film_fingerprint(film)
        relative = film.relative_to(volume.root).as_posix()
        with _transaction(self.conn):
            self.conn.execute(
                "INSERT INTO assets (source_id, sha256, path, media_kind, rights_type, "
                "attribution) VALUES (?, ?, ?, 'video', 'film_reference', ?) "
                "ON CONFLICT(sha256) DO UPDATE SET path = excluded.path",
                (source.id, sha, relative, film_title(film)),
            )
            asset_id = self.conn.execute(
                "SELECT id FROM assets WHERE sha256 = ?", (sha,)
            ).fetchone()[0]
            self.conn.execute("INSERT OR IGNORE INTO film_index (asset_id) VALUES (?)", (asset_id,))
        return int(asset_id)

    def _probe(self, asset_id: int, film: Path) -> None:
        try:
            info = probe(film, self.deps.run)
        except Exception as exc:  # ffprobe on a broken or partial file
            raise FilmIndexError(f"probe failed: {exc}") from exc
        self.conn.execute(
            "UPDATE assets SET duration = ?, width = ?, height = ?, fps = ? WHERE id = ?",
            (info.duration, info.width, info.height, info.fps, asset_id),
        )

    def _transcript(self, asset_id: int, film: Path) -> None:
        sidecar = find_sidecar(film)
        if sidecar is not None:
            words, model, language = parse_subtitles(sidecar), "subtitles:sidecar", None
        elif (stream := text_subtitle_stream(film, self.deps.run)) is not None:
            self.work.mkdir(parents=True, exist_ok=True)
            out = self.work / f"film-{asset_id}.srt"
            try:
                _ffmpeg(["ffmpeg", "-v", "error", "-y", "-i", str(film), "-map",
                         f"0:{stream}", "-f", "srt", str(out)], self.deps.run,
                        "subtitle extraction")  # fmt: skip
                words, model, language = parse_subtitles(out), "subtitles:embedded", None
            finally:
                out.unlink(missing_ok=True)
        else:
            self.work.mkdir(parents=True, exist_ok=True)
            wav = self.work / f"film-{asset_id}.wav"
            try:
                _ffmpeg(["ffmpeg", "-v", "error", "-y", "-i", str(film), "-vn", "-ac", "1",
                         "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], self.deps.run,
                        "audio extraction")  # fmt: skip
                lock = (GpuLock(self.deps.gpu_lock, job=f"index film {asset_id}")
                        if self.deps.gpu_lock else nullcontext())  # fmt: skip
                with lock:  # NFR-6: one GPU job at a time, per film
                    heard = self.deps.transcriber().transcribe(wav)
                words, model, language = heard.words, f"whisper:{heard.model}", heard.language
            finally:
                wav.unlink(missing_ok=True)
        timed = [{"text": w.text, "start": w.start, "end": w.end} for w in words]
        self.conn.execute(
            "INSERT OR REPLACE INTO transcripts (asset_id, model, language, words) "
            "VALUES (?, ?, ?, ?)",
            (asset_id, model, language, json.dumps(timed)),
        )

    def _scenes(self, asset_id: int, film: Path) -> None:
        duration = self.conn.execute(
            "SELECT duration FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()[0]
        detect_scenes(self.conn, asset_id, film, duration, self.keyframes / str(asset_id),
                      self.deps.run)  # fmt: skip

    def _chunks(self, asset_id: int, film: Path) -> None:
        row = self.conn.execute(
            "SELECT words FROM transcripts WHERE asset_id = ? ORDER BY created_at DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
        words = [Word(w["text"], w["start"], w["end"]) for w in json.loads(row[0])] if row else []
        store_chunks(self.conn, asset_id, words, self.deps.embedder)

    def _images(self, asset_id: int, film: Path) -> None:
        embed_keyframes(self.conn, asset_id, self.deps.clip)

    # the night ---------------------------------------------------------------------------

    def _stop_reason(self, until: datetime | None) -> str | None:
        if until is not None and self.deps.clock() >= until:
            return f"deadline {until:%H:%M} reached"
        return disk_preflight(self.deps.disk_free(self.media_root),
                              self.config.engine.disk.stop_free_gb)  # fmt: skip

    def run(self, volumes: list[Volume], until: datetime | None = None,
            retry_failed: bool = False) -> IndexReport:  # fmt: skip
        report = IndexReport()
        sync_sources(self.conn, self.config)
        drives = [s for s in self.config.sources
                  if s.kind == "drive" and s.rights_type == "film_reference"]  # fmt: skip
        for source in drives:
            volume = find_volume(source, volumes)
            if volume is None:
                report.missing_drives.append(source.id)
                continue
            for film in discover(volume.root / (source.path or "")):
                if reason := self._stop_reason(until):
                    report.stopped = reason
                    return report
                report.films.append(self._index_one(source, volume, film, until, retry_failed))
                if report.films[-1].skipped and report.films[-1].skipped.startswith("deadline"):
                    report.stopped = report.films[-1].skipped
                    return report
        return report

    def _index_one(self, source: SourceConfig, volume: Volume, film: Path,
                   until: datetime | None, retry_failed: bool) -> FilmResult:  # fmt: skip
        result = FilmResult(film_title(film), None)
        try:
            asset_id = self.register(source, volume, film)
        except OSError as exc:
            result.error = f"unreadable: {exc}"
            return result
        result.asset_id = asset_id
        status = self.conn.execute(
            "SELECT * FROM film_index WHERE asset_id = ?", (asset_id,)
        ).fetchone()
        if status["error"] and not retry_failed:
            result.skipped = f"failed before at {status['error_step']}: {status['error']}"
            return result
        steps = {"probe": self._probe, "transcript": self._transcript,
                 "scenes": self._scenes, "chunks": self._chunks,
                 "images": self._images}  # fmt: skip
        for step in STEPS:
            if status[f"{step}_at" if step != "probe" else "probed_at"]:
                continue
            if step == "images" and self.deps.clip is None:
                continue  # waits for the CLIP model
            if reason := self._stop_reason(until):
                result.skipped = reason
                return result
            column = "probed_at" if step == "probe" else f"{step}_at"
            try:
                with _transaction(self.conn):
                    steps[step](asset_id, film)
                    self.conn.execute(
                        f"UPDATE film_index SET {column} = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),"
                        " error = NULL, error_step = NULL, updated_at = strftime("
                        "'%Y-%m-%dT%H:%M:%SZ', 'now') WHERE asset_id = ?",
                        (asset_id,),
                    )
            except (FilmIndexError, ValueError, OSError) as exc:
                self.conn.execute(
                    "UPDATE film_index SET error_step = ?, error = ?, updated_at = "
                    "strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE asset_id = ?",
                    (step, str(exc)[:500], asset_id),
                )
                result.error = f"{step}: {exc}"
                return result
            result.done.append(step)
        return result
