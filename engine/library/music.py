"""The music pool (SRD FR-ING-6, FR-SRC-2, C14). ADR 0023.

Tracks enter only from library sources with a path in the sources config (Phase 0: the
YouTube Audio Library tracks Abbas downloads into `media/library/music`, G0.9b). Each audio
file is hashed and deduped like footage, read in place, and imported once:
- duration and the title and artist tags (ffprobe; the file name when there is no title);
- tempo from the first 90 s (the style analyser's onset tempo) and the look's energy band;
- a licence record carrying the source's licence URL, terms (its rights proof) and credit,
  with the artist as creator.
Each scan copies the source's licence onto its tracks again, so a changed licence reaches
tracks already imported. A file that fails is recorded and skipped until `--retry-failed`;
the scan goes on with the next one.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from engine.config import Config
from engine.config.models import SourceConfig
from engine.library.assets import file_sha256
from engine.library.film_index import discover
from engine.library.ingest import known_path, register_file, source_root, transaction
from engine.library.sources import sync_sources
from engine.produce.process import Runner, output_tail, run_command
from engine.style.look import energy_band
from engine.style.measure import tempo_bpm
from engine.style.profile import Reader, read_bytes

AUDIO_SUFFIXES = frozenset({".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"})
TEMPO_RATE = 22050
TEMPO_SECONDS = 90
NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


@dataclass
class TrackResult:
    path: str
    asset_id: int | None = None
    imported: bool = False
    error: str | None = None
    skipped: str | None = None


@dataclass
class MusicReport:
    tracks: list[TrackResult] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TrackFacts:
    duration: float
    title: str
    artist: str | None
    tempo_bpm: float | None


def music_sources(config: Config) -> list[SourceConfig]:
    """Library sources with a local path: music pools (stock APIs have a url instead)."""
    return [s for s in config.sources if s.kind == "library" and s.path]


class MusicImporter:
    def __init__(self, conn: sqlite3.Connection, config: Config, run: Runner = run_command,
                 read: Reader = read_bytes,
                 hasher: Callable[[Path], str] = file_sha256) -> None:  # fmt: skip
        self.conn, self.config, self.run_tool, self.read, self.hasher = (
            conn, config, run, read, hasher)  # fmt: skip

    def run(self, source_id: str | None = None, retry_failed: bool = False) -> MusicReport:
        sources = music_sources(self.config)
        if source_id is not None:
            sources = [s for s in sources if s.id == source_id]
            if not sources:
                names = ", ".join(s.id for s in music_sources(self.config)) or "none"
                raise ValueError(f"'{source_id}' is not a music pool (they are: {names})")
        report = MusicReport()
        sync_sources(self.conn, self.config)
        for source in sources:
            self._refresh_licence(source)
            root = source_root(source)
            if not root.is_dir():
                report.missing.append(source.id)
                continue
            for path in discover(root, AUDIO_SUFFIXES):
                report.tracks.append(self._import_one(source, path, retry_failed))
        return report

    def _refresh_licence(self, source: SourceConfig) -> None:
        """FR-SRC-2: the source's licence on all its tracks and their licence records."""
        expires = source.expires_at.isoformat() if source.expires_at else None
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE assets SET licence_url = ?, attribution = ?, licence_expires_at = ? "
                "WHERE source_id = ?",
                (source.licence_url, source.attribution, expires, source.id),
            )
            self.conn.execute(
                "UPDATE asset_licences SET licence_url = ?, terms = ?, attribution = ? "
                "WHERE asset_id IN (SELECT id FROM assets WHERE source_id = ?)",
                (source.licence_url, source.rights_proof, source.attribution or "", source.id),
            )

    def measure(self, path: Path) -> TrackFacts:
        argv = ["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags",
                "-of", "json", str(path)]  # fmt: skip
        result = self.run_tool(argv, None)
        if result.returncode != 0:
            raise ValueError(f"ffprobe failed: {output_tail(result)}")
        fmt = json.loads(result.stdout).get("format", {})
        if "duration" not in fmt:
            raise ValueError("ffprobe found no duration")
        tags = {k.lower(): str(v).strip() for k, v in (fmt.get("tags") or {}).items()}
        argv = ["ffmpeg", "-v", "error", "-t", str(TEMPO_SECONDS), "-i", str(path), "-vn",
                "-ac", "1", "-ar", str(TEMPO_RATE), "-f", "s16le", "-"]  # fmt: skip
        raw = self.read(argv)
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
        return TrackFacts(duration=float(fmt["duration"]), title=tags.get("title") or path.stem,
                          artist=tags.get("artist") or None,
                          tempo_bpm=tempo_bpm(samples, TEMPO_RATE))  # fmt: skip

    def _import_one(self, source: SourceConfig, path: Path, retry_failed: bool) -> TrackResult:
        result = TrackResult(str(path))
        try:
            asset_id = known_path(self.conn, path)
            if asset_id is None:
                sha = self.hasher(path)
                with transaction(self.conn):
                    asset_id = register_file(self.conn, source, path, sha, "audio", "none")
        except OSError as exc:
            result.error = f"unreadable: {exc}"
            return result
        result.asset_id = asset_id
        owner = self.conn.execute("SELECT source_id FROM assets WHERE id = ?",
                                  (asset_id,)).fetchone()[0]  # fmt: skip
        if owner != source.id:
            result.skipped = f"the same bytes are asset {asset_id} of source '{owner}'"
            return result
        row = self.conn.execute("SELECT imported_at, error FROM tracks WHERE asset_id = ?",
                                (asset_id,)).fetchone()  # fmt: skip
        if row is not None and row["imported_at"]:
            return result
        if row is not None and row["error"] and not retry_failed:
            result.skipped = f"failed before: {row['error']}"
            return result
        try:
            facts = self.measure(path)
        except Exception as exc:  # recorded, never fatal: the scan goes on with the next file
            with transaction(self.conn):
                self.conn.execute(
                    f"INSERT INTO tracks (asset_id, title, error, updated_at) "
                    f"VALUES (?, ?, ?, {NOW}) ON CONFLICT(asset_id) DO UPDATE SET "
                    f"error = excluded.error, updated_at = excluded.updated_at",
                    (asset_id, path.stem, str(exc)[:500]),
                )
            result.error = str(exc)
            return result
        energy = energy_band(facts.tempo_bpm) if facts.tempo_bpm else None
        with transaction(self.conn):
            self.conn.execute("UPDATE assets SET duration = ? WHERE id = ?",
                              (facts.duration, asset_id))  # fmt: skip
            self.conn.execute(
                f"INSERT INTO tracks (asset_id, title, artist, tempo_bpm, energy, imported_at, "
                f"error, updated_at) VALUES (?, ?, ?, ?, ?, {NOW}, NULL, {NOW}) "
                f"ON CONFLICT(asset_id) DO UPDATE SET title = excluded.title, "
                f"artist = excluded.artist, tempo_bpm = excluded.tempo_bpm, "
                f"energy = excluded.energy, imported_at = excluded.imported_at, error = NULL, "
                f"updated_at = excluded.updated_at",
                (asset_id, facts.title, facts.artist, facts.tempo_bpm, energy),
            )
            self.conn.execute(
                "INSERT INTO asset_licences (asset_id, licence_url, terms, attribution, creator) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(asset_id) DO UPDATE SET "
                "licence_url = excluded.licence_url, terms = excluded.terms, "
                "attribution = excluded.attribution, creator = excluded.creator",
                (asset_id, source.licence_url, source.rights_proof, source.attribution or "",
                 facts.artist),
            )  # fmt: skip
        result.imported = True
        return result
