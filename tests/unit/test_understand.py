"""Shared understanding steps (FR-UND-2, FR-UND-3, FR-UND-5) on generated clips: scenes and
keyframes, transcript chunks with text vectors, keyframe vectors, and the face sampler."""

from pathlib import Path

import pytest
from library_fixtures import make_clip

from engine.db import open_db
from engine.library.clip import FakeClip
from engine.library.embeddings import FakeEmbedder
from engine.library.faces import FakeFaces
from engine.library.transcribe import Word
from engine.library.understand import count_faces, detect_scenes, embed_keyframes, store_chunks
from engine.produce.ffmpeg import probe
from engine.produce.process import run_command

pytestmark = pytest.mark.media  # draws or probes clips with ffmpeg (ADR 0024)


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> dict[str, Path]:
    folder = tmp_path_factory.mktemp("clips")
    return {
        "two": make_clip(folder / "two.mp4", ["ocean", "forest"]),
        "short": make_clip(folder / "short.mp4", ["night"], seconds_each=0.8),
        "person": make_clip(folder / "person.mp4", ["sunrise", "sunrise", "person"]),
    }


@pytest.fixture
def conn(tmp_path):
    db = open_db(tmp_path / "engine.db")
    db.execute(
        "INSERT INTO sources (id, kind, path, rights_type, rights_proof) "
        "VALUES ('own', 'folder', 'x', 'own', 'Drawn for the tests')"
    )
    return db


def asset(conn, path: Path) -> int:
    return conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, rights_type) "
        "VALUES ('own', ?, ?, 'video', 'own')",
        (path.stem[0] * 64, str(path)),
    ).lastrowid


def scenes(conn, asset_id: int) -> list:
    return conn.execute(
        "SELECT id, start_s, end_s, keyframe_path, image_model FROM scenes WHERE asset_id = ? "
        "ORDER BY start_s",
        (asset_id,),
    ).fetchall()


def count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_one_keyframe_per_scene(conn, clips, tmp_path):
    a = asset(conn, clips["two"])
    assert detect_scenes(conn, a, clips["two"], 4.0, tmp_path / "kf", run_command) == 2
    rows = scenes(conn, a)
    assert [(round(r["start_s"], 1), round(r["end_s"], 1)) for r in rows] == [(0, 2), (2, 4)]
    assert all(Path(r["keyframe_path"]).read_bytes()[:2] == b"\xff\xd8" for r in rows)


def test_scenes_rebuilt_drop_old_vectors(conn, clips, tmp_path):
    a = asset(conn, clips["two"])
    detect_scenes(conn, a, clips["two"], 4.0, tmp_path / "kf", run_command)
    old = {r["id"] for r in scenes(conn, a)}
    assert embed_keyframes(conn, a, FakeClip()) == 2
    assert {r[0] for r in conn.execute("SELECT rowid FROM image_vec")} == old
    detect_scenes(conn, a, clips["two"], 4.0, tmp_path / "kf", run_command)
    assert count(conn, "image_vec") == 0 and len(scenes(conn, a)) == 2


def test_short_clip_without_cuts_is_one_scene(conn, clips, tmp_path):
    duration = probe(clips["short"]).duration
    a = asset(conn, clips["short"])
    assert detect_scenes(conn, a, clips["short"], duration, tmp_path / "kf", run_command) == 1
    (row,) = scenes(conn, a)
    assert row["start_s"] == 0 and row["end_s"] == pytest.approx(0.8, abs=0.05)


def test_embed_keyframes_sets_the_model(conn, clips, tmp_path):
    a = asset(conn, clips["two"])
    detect_scenes(conn, a, clips["two"], 4.0, tmp_path / "kf", run_command)
    embed_keyframes(conn, a, FakeClip())
    assert {r["image_model"] for r in scenes(conn, a)} == {FakeClip.name}


def test_store_chunks_writes_text_vectors_and_replaces_them(conn, clips):
    a = asset(conn, clips["two"])
    words = [Word("start", 0.0, 0.5), Word("small", 0.5, 1.0)]
    assert store_chunks(conn, a, words, FakeEmbedder()) == 1
    assert store_chunks(conn, a, words, FakeEmbedder()) == 1
    ids = [r[0] for r in conn.execute("SELECT id FROM transcript_chunks")]
    assert [r[0] for r in conn.execute("SELECT rowid FROM text_vec")] == ids and len(ids) == 1


def test_store_chunks_without_words_writes_nothing(conn, clips):
    assert store_chunks(conn, asset(conn, clips["two"]), [], FakeEmbedder()) == 0
    assert count(conn, "text_vec") == 0


def test_count_faces_samples_every_second(conn, clips, tmp_path):
    a = asset(conn, clips["person"])
    detect_scenes(conn, a, clips["person"], 6.0, tmp_path / "kf", run_command)
    keyframes = [(Path(r["keyframe_path"]), (r["start_s"] + r["end_s"]) / 2)
                 for r in scenes(conn, a)]  # fmt: skip
    frames, first = count_faces(clips["person"], keyframes, FakeFaces(), run_command,
                                tmp_path / "work")  # fmt: skip
    assert first == pytest.approx(4.0, abs=0.05)
    assert frames == 3  # the samples at 4 s and 5 s, and the second scene's keyframe at 5 s
    assert not (tmp_path / "work").exists()  # sampled frames and their folder are deleted


def test_count_faces_finds_none_in_scenery(clips, tmp_path):
    assert count_faces(clips["two"], [], FakeFaces(), run_command, tmp_path / "w") == (0, None)


def test_a_check_that_saw_no_frame_fails_instead_of_passing(clips, tmp_path):
    """A face check that examined nothing must not read as 'no faces' (§8.3 fails closed)."""
    import subprocess

    from engine.library.understand import StepError

    def silent(argv, cwd):  # ffmpeg 'succeeds' but writes no frame
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(StepError, match="no frame"):
        count_faces(clips["two"], [], FakeFaces(), silent, tmp_path / "w")


def test_stale_frames_from_a_killed_run_are_not_counted(clips, tmp_path):
    from PIL import Image

    work = tmp_path / "w"
    work.mkdir()
    Image.new("RGB", (64, 36), (255, 0, 255)).save(work / "face-00099.png")  # magenta: a face
    assert count_faces(clips["two"], [], FakeFaces(), run_command, work) == (0, None)
