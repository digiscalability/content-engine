"""The people rule's review list and Abbas's marks (SRD FR-UND-5, §8.3, C13)."""

import shutil
from dataclasses import replace

import pytest
from library_fixtures import make_clip

from engine.config import load_config
from engine.config.models import SourceConfig
from engine.db import open_db
from engine.library.clip import FakeClip
from engine.library.embeddings import FakeEmbedder
from engine.library.faces import FakeFaces
from engine.library.ingest import IngestDeps, Ingester
from engine.library.people import mark, review_list


@pytest.fixture
def conn(tmp_path):
    db = open_db(tmp_path / "engine.db")
    db.execute(
        "INSERT INTO sources (id, kind, path, rights_type, rights_proof) "
        "VALUES ('own-clips', 'folder', 'x', 'own', 'Drawn for the tests')"
    )
    return db


def seed(conn, name: str, release: str, checked: bool, faces: int = 0) -> int:
    asset_id = conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, duration, rights_type, "
        "people_release) VALUES ('own-clips', ?, ?, 'video', 4.0, 'own', ?)",
        (name[0] * 64, f"C:/clips/{name}", release),
    ).lastrowid
    conn.execute(
        "INSERT INTO asset_index (asset_id, people_at, faces, face_first_s) VALUES (?, ?, ?, ?)",
        (asset_id, "2026-11-13T00:00:00Z" if checked else None, faces, 2.0 if faces else None),
    )
    conn.execute(
        "INSERT INTO scenes (asset_id, start_s, end_s, keyframe_path) VALUES (?, 0, 4, ?)",
        (asset_id, f"C:/kf/{asset_id}.jpg"),
    )
    return asset_id


def test_review_list_lists_checked_unmarked_faces_only(conn):
    friend = seed(conn, "friend.mp4", "unknown", checked=True, faces=3)
    seed(conn, "scenery.mp4", "none", checked=True)
    seed(conn, "unchecked.mp4", "unknown", checked=False)
    seed(conn, "released.mp4", "yes", checked=True, faces=1)
    (item,) = review_list(conn)
    assert item.asset_id == friend and item.source_id == "own-clips"
    assert item.path == "C:/clips/friend.mp4" and item.faces == 3
    assert item.first_face_s == 2.0 and item.keyframe == f"C:/kf/{friend}.jpg"


def test_mark_records_the_decision_and_the_state(conn):
    friend = seed(conn, "friend.mp4", "unknown", checked=True, faces=3)
    mark(conn, friend, "yes", "consent from Sam, 13 Nov")
    assert (
        conn.execute("SELECT people_release FROM assets WHERE id = ?", (friend,)).fetchone()[0]
        == "yes"
    )
    row = conn.execute("SELECT decision, note FROM people_marks WHERE asset_id = ?",
                       (friend,)).fetchone()  # fmt: skip
    assert tuple(row) == ("yes", "consent from Sam, 13 Nov")
    assert review_list(conn) == []
    mark(conn, friend, "exclude")
    assert [r[0] for r in conn.execute("SELECT decision FROM people_marks ORDER BY id")] == [
        "yes", "exclude"
    ]  # fmt: skip


def test_mark_rejects_an_unknown_asset_or_decision(conn):
    friend = seed(conn, "friend.mp4", "unknown", checked=True, faces=3)
    with pytest.raises(ValueError, match="no asset 999"):
        mark(conn, 999, "yes")
    with pytest.raises(ValueError, match="yes or exclude"):
        mark(conn, friend, "none")
    assert conn.execute("SELECT count(*) FROM people_marks").fetchone()[0] == 0


class NoFaces:
    name = "sees-nothing"

    def count(self, image) -> int:
        return 0


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_a_mark_survives_a_new_people_check(tmp_path, repo_root):
    """A re-run of the people step, even one that sees no face, never undoes Abbas's mark."""
    folder = tmp_path / "clips"
    folder.mkdir()
    make_clip(folder / "friend.mp4", ["person"])
    shutil.copy2(folder / "friend.mp4", tmp_path / "unused.mp4")
    own = SourceConfig(id="own-clips", kind="folder", path=str(folder), rights_type="own",
                       rights_proof="Drawn for the tests")  # fmt: skip
    config = replace(load_config(repo_root / "config"), sources=[own])
    deps = IngestDeps(embedder=FakeEmbedder(), clip=FakeClip(), faces=FakeFaces(),
                      transcriber=lambda: None)  # fmt: skip
    ingester = Ingester(open_db(tmp_path / "engine.db"), config, deps, tmp_path / "media")
    (result,) = ingester.run().assets
    assert [i.asset_id for i in review_list(ingester.conn)] == [result.asset_id]
    mark(ingester.conn, result.asset_id, "exclude")
    ingester.conn.execute("UPDATE asset_index SET people_at = NULL")
    ingester.deps.faces = NoFaces()
    ingester.run()
    release = ingester.conn.execute(
        "SELECT people_release FROM assets WHERE id = ?", (result.asset_id,)
    ).fetchone()[0]
    assert release == "exclude"
