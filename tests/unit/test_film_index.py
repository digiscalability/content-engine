"""The film drive indexer (SRD §8.2): in place, read-only, resumable. Tiny films made by
ffmpeg stand in for a drive; Whisper and the text model are fakes."""

import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from engine.config import load_config
from engine.config.models import SourceConfig
from engine.db import open_db
from engine.library.clip import FakeClip
from engine.library.embeddings import FakeEmbedder
from engine.library.film_index import (
    FilmIndexer,
    IndexDeps,
    chunk_words,
    discover,
    film_fingerprint,
    film_title,
    find_sidecar,
    scenes_from_cuts,
)
from engine.library.transcribe import Transcript, Word
from engine.library.volumes import Volume

SRT = (
    "1\n00:00:00,500 --> 00:00:02,000\nYou start small.\n\n"
    "2\n00:00:03,000 --> 00:00:05,500\nThen again.\n"
)
NIGHT = datetime(2026, 11, 14, 1, 0)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def films(tmp_path_factory) -> Path:
    """Three 6 s films of three colour scenes: one with a sidecar, one with embedded
    subtitles, one with none; and one broken file."""
    folder = tmp_path_factory.mktemp("films")
    colours = ["black", "white", "black"]  # cuts well above ffmpeg's 0.3 scene score
    inputs = []
    for colour in colours:
        inputs += ["-f", "lavfi", "-i", f"color=c={colour}:s=160x90:d=2:r=24"]
    graph = "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]"
    base = folder / "base.mp4"
    ffmpeg(*inputs, "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
           "-filter_complex", graph, "-map", "[v]", "-map", "3:a", "-c:v", "libx264",
           "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(base))  # fmt: skip
    shutil.copy(base, folder / "Heat.1995.1080p.mp4")
    (folder / "Heat.1995.1080p.en.srt").write_text(SRT, encoding="utf-8")
    srt = folder / "embedded.srt"
    srt.write_text(SRT, encoding="utf-8")
    ffmpeg("-i", str(base), "-i", str(srt), "-map", "0", "-map", "1", "-c", "copy",
           "-c:s", "srt", "-metadata:s:s:0", "language=eng",
           str(folder / "Other (2001).mkv"))  # fmt: skip
    ffmpeg("-i", str(base), "-vf", "negate", "-c:v", "libx264", "-preset", "ultrafast",
           "-c:a", "copy", str(folder / "Quiet Film 2010.mp4"))  # fmt: skip
    (folder / "broken.mkv").write_bytes(b"not a film" * 1000)
    srt.unlink()
    base.unlink()
    return folder


class FakeWhisper:
    calls = 0

    def transcribe(self, audio: Path) -> Transcript:
        FakeWhisper.calls += 1
        assert audio.is_file()
        return Transcript("en", "fake", [Word("heard", 0.5, 1.0), Word("words", 1.0, 1.5)])


@pytest.fixture
def world(tmp_path, films, repo_root):
    drive = tmp_path / "drive"
    shutil.copytree(films, drive / "Movies")
    (drive / "$RECYCLE.BIN").mkdir()
    shutil.copy(films / "Heat.1995.1080p.mp4", drive / "$RECYCLE.BIN" / "old.mp4")
    source = SourceConfig(
        id="films", kind="drive", rights_type="film_reference", path="Movies",
        rights_proof="Abbas's own film collection, used only under §8.2",
        volume_serial="1A2B-3C4D", volume_label="FILMS",
    )  # fmt: skip
    config = replace(load_config(repo_root / "config"), sources=[source])
    conn = open_db(tmp_path / "engine.db")
    FakeWhisper.calls = 0
    deps = IndexDeps(
        embedder=FakeEmbedder(), transcriber=FakeWhisper, clock=lambda: NIGHT, clip=FakeClip()
    )
    indexer = FilmIndexer(conn, config, deps, tmp_path / "media")
    return indexer, [Volume(drive, "1a2b3c4d", "films")], drive


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}  # fmt: skip


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_each_film_is_indexed_in_place(world):
    indexer, volumes, _ = world
    report = indexer.run(volumes)
    by_title = {f.title: f for f in report.films}
    assert set(by_title) == {"Heat (1995)", "Other (2001)", "Quiet Film (2010)", "broken"}
    for title in ("Heat (1995)", "Other (2001)", "Quiet Film (2010)"):
        assert by_title[title].done == ["probe", "transcript", "scenes", "chunks", "images"], title
    conn = indexer.conn
    heat = conn.execute("SELECT * FROM assets WHERE attribution = 'Heat (1995)'").fetchone()
    assert heat["path"] == "Movies/Heat.1995.1080p.mp4" and heat["rights_type"] == "film_reference"
    assert heat["duration"] == pytest.approx(6.0, abs=0.1)
    models = dict(conn.execute(
        "SELECT a.attribution, t.model FROM transcripts t JOIN assets a ON a.id = t.asset_id"
    ).fetchall())  # fmt: skip
    assert models == {"Heat (1995)": "subtitles:sidecar", "Other (2001)": "subtitles:embedded",
                      "Quiet Film (2010)": "whisper:fake"}  # fmt: skip
    scenes = conn.execute(
        "SELECT start_s, end_s, keyframe_path FROM scenes WHERE asset_id = ? ORDER BY start_s",
        (heat["id"],),
    ).fetchall()
    assert [(round(s, 1), round(e, 1)) for s, e, _ in scenes] == [(0, 2), (2, 4), (4, 6)]
    assert all(Path(k).is_file() and Path(k).read_bytes()[:2] == b"\xff\xd8" for *_, k in scenes)
    chunk = conn.execute("SELECT * FROM transcript_chunks WHERE asset_id = ?", (heat["id"],))
    row = chunk.fetchone()
    assert row["text"] == "You start small. Then again." and len(row["embedding"]) == 384 * 4
    assert conn.execute("SELECT rowid FROM text_vec WHERE rowid = ?", (row["id"],)).fetchone()
    vectors = conn.execute(
        "SELECT count(*) FROM image_vec v JOIN scenes s ON s.id = v.rowid WHERE s.asset_id = ?",
        (heat["id"],),
    ).fetchone()[0]
    assert vectors == 3


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_a_film_indexed_before_gets_only_its_keyframe_vectors(world):
    indexer, volumes, _ = world
    indexer.deps.clip = None  # as P1.6 left it: no CLIP step yet
    first = indexer.run(volumes)
    heat = next(f for f in first.films if f.title == "Heat (1995)")
    assert heat.done == ["probe", "transcript", "scenes", "chunks"]
    indexer.deps.clip = FakeClip()
    again = indexer.run(volumes)
    assert next(f for f in again.films if f.title == "Heat (1995)").done == ["images"]


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_whisper_runs_only_without_subtitles_and_leaves_no_audio(world):
    indexer, volumes, _ = world
    indexer.run(volumes)
    assert FakeWhisper.calls == 1
    assert not list(indexer.work.glob("*")) if indexer.work.exists() else True


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_indexing_never_writes_to_the_drive(world):
    indexer, volumes, drive = world
    before = snapshot(drive)
    indexer.run(volumes)
    assert snapshot(drive) == before


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_a_rerun_resumes_and_adds_nothing_twice(world):
    indexer, volumes, _ = world
    ticks = iter([NIGHT] * 6 + [NIGHT + timedelta(hours=6)] * 100)
    indexer.deps.clock = lambda: next(ticks)
    first = indexer.run(volumes, until=NIGHT + timedelta(hours=5))
    assert first.stopped and first.stopped.startswith("deadline")
    partial = sum(len(f.done) for f in first.films)
    assert 0 < partial < 15  # three films, five steps each
    indexer.deps.clock = lambda: NIGHT
    second = indexer.run(volumes, until=NIGHT + timedelta(hours=5))
    assert sum(len(f.done) for f in second.films) == 15 - partial
    counts = {t: count(indexer.conn, t) for t in ("assets", "transcripts", "transcript_chunks")}
    third = indexer.run(volumes)
    assert all(f.done == [] for f in third.films)
    assert {t: count(indexer.conn, t) for t in counts} == counts
    assert count(indexer.conn, "assets") == 4  # three films and the broken file, once each


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_a_broken_film_is_recorded_and_skipped(world):
    indexer, volumes, _ = world
    broken = next(f for f in indexer.run(volumes).films if f.title == "broken")
    assert broken.error and broken.error.startswith("probe")
    again = next(f for f in indexer.run(volumes).films if f.title == "broken")
    assert again.skipped and "failed before at probe" in again.skipped
    retried = next(f for f in indexer.run(volumes, retry_failed=True).films if f.title == "broken")
    assert retried.error and not retried.skipped


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_the_run_stops_at_the_deadline(world):
    indexer, volumes, _ = world
    report = indexer.run(volumes, until=NIGHT - timedelta(minutes=1))
    assert report.stopped.startswith("deadline") and report.films == []
    assert count(indexer.conn, "assets") == 0


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_a_full_disk_stops_the_run(world):
    indexer, volumes, _ = world
    indexer.deps.disk_free = lambda path: 1024**3
    assert "NFR-5" in indexer.run(volumes).stopped


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_a_drive_that_is_not_connected_is_reported(world):
    indexer, _, _ = world
    report = indexer.run([Volume(Path("E:/"), "1A2B-3C4D", "BACKUP")])
    assert report.missing_drives == ["films"] and report.films == []


# Pure helpers --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "title"),
    [
        ("Heat.1995.1080p.BluRay.x264.mkv", "Heat (1995)"),
        ("The Godfather (1972).mkv", "The Godfather (1972)"),
        ("2001.A.Space.Odyssey.1968.mkv", "2001 A Space Odyssey (1968)"),
        ("home_video.mp4", "home video"),
    ],
)
def test_titles_come_from_file_names(name, title):
    assert film_title(Path(name)) == title


def test_the_fingerprint_samples_and_follows_content(tmp_path):
    big = tmp_path / "a.mkv"
    big.write_bytes(bytes(20 * 1024 * 1024))
    moved = tmp_path / "sub" / "renamed.mkv"
    moved.parent.mkdir()
    shutil.copy(big, moved)
    assert film_fingerprint(big) == film_fingerprint(moved)
    data = bytearray(big.read_bytes())
    data[10 * 1024 * 1024] = 1  # the middle sample
    big.write_bytes(bytes(data))
    assert film_fingerprint(big) != film_fingerprint(moved)


def test_scenes_merge_cuts_closer_than_a_second():
    assert scenes_from_cuts([2.0, 2.4, 4.0, 5.8], 6.0) == [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]
    assert scenes_from_cuts([], 6.0) == [(0.0, 6.0)]


def test_chunks_are_about_thirty_seconds():
    words = [Word(f"w{i}", i * 2.0, i * 2.0 + 1.5) for i in range(40)]  # 80 s of speech
    chunks = chunk_words(words)
    assert len(chunks) == 3 and chunks[0][0] == 0.0 and chunks[0][1] >= 30.0
    assert " ".join(c[2] for c in chunks).split() == [w.text for w in words]


def test_english_sidecars_come_first(tmp_path):
    film = tmp_path / "Heat.1995.mkv"
    film.write_bytes(b"x")
    for name in ("Heat.1995.fr.srt", "Heat.1995.en.srt", "Other.srt"):
        (tmp_path / name).write_text(SRT, encoding="utf-8")
    assert find_sidecar(film).name == "Heat.1995.en.srt"


def test_discover_skips_system_folders(tmp_path):
    (tmp_path / "$RECYCLE.BIN").mkdir()
    (tmp_path / "$RECYCLE.BIN" / "x.mkv").write_bytes(b"x")
    (tmp_path / "A").mkdir()
    (tmp_path / "A" / "film.MKV").write_bytes(b"x")
    (tmp_path / "A" / "notes.txt").write_bytes(b"x")
    assert [p.name for p in discover(tmp_path)] == ["film.MKV"]


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 14, 0, 0), datetime(2026, 11, 14, 6, 0)),
        (datetime(2026, 11, 13, 23, 30), datetime(2026, 11, 14, 6, 0)),
        (datetime(2026, 11, 14, 7, 0), None),  # a missed run caught up at 07:00 does nothing
    ],
)
def test_the_deadline_is_the_next_one_inside_the_night(now, expected):
    from engine.library.film_index import next_deadline

    assert next_deadline("06:00", now) == expected


def test_a_bad_deadline_is_refused():
    from engine.library.film_index import next_deadline

    with pytest.raises(ValueError, match="HH:MM"):
        next_deadline("6am", NIGHT)
