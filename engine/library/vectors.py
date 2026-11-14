"""The library's vectors in sqlite-vec (SRD FR-UND-3, §11; ADR 0006, ADR 0022).

`text_vec` rows share their rowid with `transcript_chunks`, `image_vec` rows with `scenes`.
Whoever rewrites an asset's chunks or scenes drops its vectors first, so no vector points at
a row that is gone or whose id was reused.
"""

from __future__ import annotations

import sqlite3

import numpy as np

from engine.library.embeddings import to_blob

TEXT_DIMS = 384  # bge-small-en-v1.5
IMAGE_DIMS = 512  # CLIP ViT-B-32


def vec_version(conn: sqlite3.Connection) -> str:
    return str(conn.execute("SELECT vec_version()").fetchone()[0])


def _checked(vector: np.ndarray, dims: int, table: str) -> bytes:
    size = int(np.asarray(vector).size)
    if size != dims:
        raise ValueError(f"{table} holds {dims}-float vectors, not {size}")
    return to_blob(vector)


def store_text(conn: sqlite3.Connection, chunk_id: int, vector: np.ndarray) -> None:
    blob = _checked(vector, TEXT_DIMS, "text_vec")
    conn.execute("DELETE FROM text_vec WHERE rowid = ?", (chunk_id,))
    conn.execute("INSERT INTO text_vec (rowid, embedding) VALUES (?, ?)", (chunk_id, blob))


def store_image(conn: sqlite3.Connection, scene_id: int, vector: np.ndarray, model: str) -> None:
    blob = _checked(vector, IMAGE_DIMS, "image_vec")
    conn.execute("DELETE FROM image_vec WHERE rowid = ?", (scene_id,))
    conn.execute("INSERT INTO image_vec (rowid, embedding) VALUES (?, ?)", (scene_id, blob))
    conn.execute("UPDATE scenes SET image_model = ? WHERE id = ?", (model, scene_id))


def drop_text_vectors(conn: sqlite3.Connection, asset_id: int) -> None:
    for (chunk_id,) in conn.execute(
        "SELECT id FROM transcript_chunks WHERE asset_id = ?", (asset_id,)
    ).fetchall():
        conn.execute("DELETE FROM text_vec WHERE rowid = ?", (chunk_id,))


def drop_image_vectors(conn: sqlite3.Connection, asset_id: int) -> None:
    for (scene_id,) in conn.execute(
        "SELECT id FROM scenes WHERE asset_id = ?", (asset_id,)
    ).fetchall():
        conn.execute("DELETE FROM image_vec WHERE rowid = ?", (scene_id,))


def drop_asset_vectors(conn: sqlite3.Connection, asset_id: int) -> None:
    drop_text_vectors(conn, asset_id)
    drop_image_vectors(conn, asset_id)
