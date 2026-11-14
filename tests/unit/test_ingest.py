"""Own-footage ingest (SRD FR-ING-1, FR-ING-4, FR-SRC-2, FR-UND-2/3/5) on generated clips:
in place, hashed and deduped, resumable. Whisper, CLIP, the text model and the face detector
are fakes."""

import os
import shutil
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from library_fixtures import make_clip

from engine.config import load_config
from engine.config.models import SourceConfig
from engine.db import open_db
from engine.library.assets import file_sha256
from engine.library.clip import FakeClip
from engine.library.embeddings import FakeEmbedder
from engine.library.faces import FakeFaces
from engine.library.ingest import ALL_STEPS, IngestDeps, Ingester
from engine.library.transcribe import Transcript, Word

pytestmark = pytest.mark.media  # draws or probes clips with ffmpeg (ADR 0024)

LICENCE = "https://www.pexels.com/license/"


@pytest.fixture(scope="module")
def footage(tmp_path_factory) -> Path:
    """An own folder (scenery, a talking clip with sound, a person, a copy, a broken file,
    a subfolder) and a stock folder."""
    root = tmp_path_factory.mktemp("footage")
    own = root / "own"
    (own / "trip").mkdir(parents=True)
    make_clip(own / "sunrise.mp4", ["sunrise"])
    make_clip(own / "ocean talk.mp4", ["ocean"], tone=True)
    make_clip(own / "two scenes.mov", ["night", "forest"])
    make_clip(own / "friend.mp4", ["sunrise", "person"])
    make_clip(own / "trip" / "notebook.mp4", ["notebook"])
    shutil.copy2(own / "sunrise.mp4", own / "trip" / "sunrise copy.mp4")
    (own / "broken.mp4").write_bytes(b"not a video" * 500)
    (own / "notes.txt").write_text("not footage", encoding="utf-8")
    make_clip(root / "stock" / "forest.mp4", ["forest"]) if (root / "stock").mkdir() is None else 0
    return root


class FakeWhisper:
    calls = 0

    def transcribe(self, audio: Path) -> Transcript:
        FakeWhisper.calls += 1
        assert audio.is_file()
        return Transcript("en", "fake", [Word("start", 0.2, 0.6), Word("small", 0.6, 1.0)])


class CountingHasher:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def __call__(self, path: Path) -> str:
        self.paths.append(path)
        return file_sha256(path)


def sources(root: Path) -> list[SourceConfig]:
    return [
        SourceConfig(id="own-clips", kind="folder", path=str(root / "own"),
                     rights_type="own", rights_proof="Drawn for the tests"),
        SourceConfig(id="stock-clips", kind="folder", path=str(root / "stock"),
                     rights_type="stock", rights_proof="Pexels licence", licence_url=LICENCE,
                     attribution="Photographer on Pexels", expires_at=date(2030, 1, 1)),
        SourceConfig(id="films", kind="drive", rights_type="film_reference",
                     rights_proof="own collection", volume_serial="1", volume_label="F"),
    ]  # fmt: skip


@pytest.fixture
def world(tmp_path, footage, repo_root):
    root = tmp_path / "footage"
    shutil.copytree(footage, root)  # copy2: sizes and mtimes kept
    config = replace(load_config(repo_root / "config"), sources=sources(root))
    FakeWhisper.calls = 0
    hasher = CountingHasher()
    deps = IngestDeps(embedder=FakeEmbedder(), clip=FakeClip(), faces=FakeFaces(),
                      transcriber=FakeWhisper, hasher=hasher)  # fmt: skip
    ingester = Ingester(open_db(tmp_path / "engine.db"), config, deps, tmp_path / "media")
    return ingester, root, hasher


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def by_name(conn) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM assets WHERE source_id <> 'engine'").fetchall()
    return {Path(r["path"]).name: dict(r) for r in rows}


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}  # fmt: skip


def test_each_clip_is_ingested_in_place(world):
    ingester, root, _ = world
    before = snapshot(root)
    report = ingester.run()
    assert snapshot(root) == before  # nothing written beside the footage
    done = {Path(a.path).name: a.done for a in report.assets if not a.error}
    for name in ("sunrise.mp4", "ocean talk.mp4", "two scenes.mov", "friend.mp4",
                 "notebook.mp4", "forest.mp4"):  # fmt: skip
        assert done[name] == list(ALL_STEPS), name
    assets = by_name(ingester.conn)
    assert assets["sunrise.mp4"]["rights_type"] == "own"
    assert assets["sunrise.mp4"]["duration"] == pytest.approx(2.0, abs=0.1)
    assert assets["sunrise.mp4"]["people_release"] == "none"
    assert assets["friend.mp4"]["people_release"] == "unknown"  # FR-UND-5
    two = assets["two scenes.mov"]["id"]
    scenes = ingester.conn.execute("SELECT id FROM scenes WHERE asset_id = ?", (two,)).fetchall()
    assert len(scenes) == 2
    vectors = {r[0] for r in ingester.conn.execute("SELECT rowid FROM image_vec")}
    assert {s["id"] for s in scenes} <= vectors
    keyframes = ingester.media_root / "index" / "own" / str(two)
    assert len(list(keyframes.glob("scene-*.jpg"))) == 2


def test_scanning_twice_adds_no_rows(world):
    """FR-ING-1's acceptance test."""
    ingester, _, _ = world
    ingester.run()
    tables = ("assets", "asset_paths", "scenes", "transcripts", "transcript_chunks", "text_vec",
              "image_vec")  # fmt: skip
    counts = {t: count(ingester.conn, t) for t in tables}
    again = ingester.run()
    assert {t: count(ingester.conn, t) for t in tables} == counts
    assert all(a.done == [] for a in again.assets if not a.skipped)


def test_same_bytes_in_two_places_are_one_asset(world):
    ingester, _, _ = world
    ingester.run()
    names = [Path(p).name for (p,) in ingester.conn.execute(
        "SELECT p.path FROM asset_paths p JOIN assets a ON a.id = p.asset_id "
        "WHERE a.path LIKE '%sunrise.mp4'")]  # fmt: skip
    assert sorted(names) == ["sunrise copy.mp4", "sunrise.mp4"]
    assert "sunrise copy.mp4" not in by_name(ingester.conn)


def test_an_unchanged_file_is_not_hashed_again(world):
    ingester, root, hasher = world
    ingester.run()
    assert len(hasher.paths) == 8  # 6 clips, the copy and the broken file
    hasher.paths.clear()
    ingester.run()
    assert hasher.paths == []
    touched = root / "own" / "sunrise.mp4"
    os.utime(touched, ns=(touched.stat().st_atime_ns, touched.stat().st_mtime_ns + 10**9))
    ingester.run()
    assert hasher.paths == [touched]


def test_stock_assets_carry_their_licence(world):
    """FR-SRC-2: licence URL, attribution and expiry come from the source."""
    ingester, _, _ = world
    ingester.run()
    forest = by_name(ingester.conn)["forest.mp4"]
    assert forest["rights_type"] == "stock" and forest["licence_url"] == LICENCE
    assert forest["attribution"] == "Photographer on Pexels"
    assert forest["licence_expires_at"] == "2030-01-01"


def test_only_clips_with_sound_are_transcribed(world):
    ingester, _, _ = world
    ingester.run()
    assert FakeWhisper.calls == 1
    talk = by_name(ingester.conn)["ocean talk.mp4"]["id"]
    (text,) = ingester.conn.execute(
        "SELECT text FROM transcript_chunks WHERE asset_id = ?", (talk,)
    ).fetchone()
    assert text == "start small"
    assert not list((ingester.media_root / "work").rglob("*.wav"))  # temporary audio removed


def test_a_disk_below_the_stop_line_stops_ingest(world):
    """FR-ING-4, NFR-5."""
    ingester, _, _ = world
    ingester.deps.disk_free = lambda path: 10 * 1024**3
    report = ingester.run()
    assert report.stopped and "stop line" in report.stopped
    assert report.assets == [] and count(ingester.conn, "asset_paths") == 0


def test_a_broken_file_is_recorded_and_skipped(world):
    ingester, _, _ = world
    first = next(a for a in ingester.run().assets if Path(a.path).name == "broken.mp4")
    assert first.error and first.error.startswith("probe")
    again = next(a for a in ingester.run().assets if Path(a.path).name == "broken.mp4")
    assert again.skipped and "probe" in again.skipped
    retried = next(a for a in ingester.run(retry_failed=True).assets
                   if Path(a.path).name == "broken.mp4")  # fmt: skip
    assert retried.error and not retried.skipped


def test_film_drives_and_other_files_are_left_alone(world):
    ingester, _, _ = world
    report = ingester.run()
    assert {Path(a.path).suffix for a in report.assets} <= {".mp4", ".mov"}
    tiers = {r[0] for r in ingester.conn.execute("SELECT DISTINCT rights_type FROM assets")}
    assert "film_reference" not in tiers


def test_one_source_at_a_time_and_unknown_ones_refused(world):
    ingester, _, _ = world
    report = ingester.run(source_id="stock-clips")
    assert [Path(a.path).name for a in report.assets] == ["forest.mp4"]
    with pytest.raises(ValueError, match="films"):
        ingester.run(source_id="films")


def test_a_missing_folder_is_reported(world):
    ingester, root, _ = world
    shutil.rmtree(root / "stock")
    assert ingester.run().missing == ["stock-clips"]


class BrokenDetector:
    name = "broken"

    def count(self, image) -> int:
        raise RuntimeError("model crashed")


def test_an_unexpected_model_error_is_recorded_and_the_run_goes_on(world):
    ingester, _, _ = world
    ingester.deps.faces = BrokenDetector()
    report = ingester.run()
    clips = [a for a in report.assets if Path(a.path).name != "broken.mp4"]
    # the copy of sunrise.mp4 is the same asset, already failed: skipped, not run again
    assert clips and all("people" in (a.error or a.skipped or "") for a in clips)
    released = {r[0] for r in ingester.conn.execute("SELECT DISTINCT people_release FROM assets")}
    assert released == {"unknown"}  # nothing was marked clean
    again = ingester.run()
    assert all(a.skipped for a in again.assets)


def test_a_changed_licence_reaches_the_ingested_clips(world):
    ingester, root, _ = world
    ingester.run()
    renewed = [s.model_copy(update={"expires_at": date(2031, 6, 30)})
               if s.id == "stock-clips" else s for s in ingester.config.sources]  # fmt: skip
    ingester.config = replace(ingester.config, sources=renewed)
    ingester.run()
    assert by_name(ingester.conn)["forest.mp4"]["licence_expires_at"] == "2031-06-30"
