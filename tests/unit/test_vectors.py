"""sqlite-vec tables for the library (SRD FR-UND-3, §11; ADR 0006 sizes): text_vec keyed to
transcript chunks, image_vec keyed to scenes."""

import shutil

import numpy as np
import pytest

from engine.db import MIGRATIONS_DIR, connect, migrate, open_db, schema_version
from engine.library.embeddings import to_blob
from engine.library.vectors import (
    IMAGE_DIMS,
    TEXT_DIMS,
    drop_asset_vectors,
    store_image,
    store_text,
    vec_version,
)


def unit(dims: int, seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).standard_normal(dims).astype(np.float32)
    return v / np.linalg.norm(v)


def seed_asset(conn, sha: str) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO sources (id, kind, path, rights_type, rights_proof) "
        "VALUES ('own', 'folder', 'x', 'own', 'Filmed by me')"
    )
    return conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, rights_type) "
        "VALUES ('own', ?, ?, 'video', 'own')",
        (sha * 64, f"{sha}.mp4"),
    ).lastrowid


def chunk(conn, asset_id: int, vector: np.ndarray) -> int:
    return conn.execute(
        "INSERT INTO transcript_chunks (asset_id, start_s, end_s, text, model, embedding) "
        "VALUES (?, 0, 1, 'words', 'bge', ?)",
        (asset_id, to_blob(vector)),
    ).lastrowid


def scene(conn, asset_id: int) -> int:
    return conn.execute(
        "INSERT INTO scenes (asset_id, start_s, end_s) VALUES (?, 0, 1)", (asset_id,)
    ).lastrowid


@pytest.fixture
def conn(tmp_path):
    return open_db(tmp_path / "engine.db")


def test_connect_loads_sqlite_vec(tmp_path):
    assert vec_version(connect(tmp_path / "e.db")).startswith("v0.1")


def test_migration_5_adds_the_library_tables(conn):
    assert schema_version(conn) >= 5
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"text_vec", "image_vec", "asset_index", "asset_paths", "people_marks"} <= names
    assert "image_model" in {r[1] for r in conn.execute("PRAGMA table_info(scenes)")}
    assert "images_at" in {r[1] for r in conn.execute("PRAGMA table_info(film_index)")}


def test_text_vec_rejects_wrong_dims(conn):
    asset = seed_asset(conn, "a")
    with pytest.raises(ValueError, match="384"):
        store_text(conn, chunk(conn, asset, unit(384, 1)), unit(1024, 1))


def test_image_vec_rejects_wrong_dims(conn):
    asset = seed_asset(conn, "a")
    with pytest.raises(ValueError, match="512"):
        store_image(conn, scene(conn, asset), unit(384, 1), "clip")


def test_store_image_records_the_model(conn):
    asset = seed_asset(conn, "a")
    sid = scene(conn, asset)
    store_image(conn, sid, unit(IMAGE_DIMS, 2), "clip-test")
    assert conn.execute("SELECT image_model FROM scenes WHERE id = ?", (sid,)).fetchone()[0] == (
        "clip-test"
    )
    assert conn.execute("SELECT count(*) FROM image_vec").fetchone()[0] == 1


def test_drop_asset_vectors_removes_only_that_asset(conn):
    a, b = seed_asset(conn, "a"), seed_asset(conn, "b")
    for asset in (a, b):
        store_text(conn, chunk(conn, asset, unit(TEXT_DIMS, asset)), unit(TEXT_DIMS, asset))
        store_image(conn, scene(conn, asset), unit(IMAGE_DIMS, asset), "clip")
    drop_asset_vectors(conn, a)
    assert conn.execute("SELECT count(*) FROM text_vec").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM image_vec").fetchone()[0] == 1
    kept = conn.execute("SELECT id FROM transcript_chunks WHERE asset_id = ?", (b,)).fetchone()[0]
    assert conn.execute("SELECT rowid FROM text_vec").fetchone()[0] == kept


def test_migration_0005_backfills_bge_chunks(tmp_path):
    first_four = tmp_path / "m"
    first_four.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("000[1-4]_*.sql")):
        shutil.copy(path, first_four / path.name)
    conn = connect(tmp_path / "e.db")
    migrate(conn, first_four)
    asset = seed_asset(conn, "a")
    bge = chunk(conn, asset, unit(384, 3))
    chunk(conn, asset, unit(1024, 4))  # a fake embedder's vector before this slice
    migrate(conn)
    assert [r[0] for r in conn.execute("SELECT rowid FROM text_vec")] == [bge]
