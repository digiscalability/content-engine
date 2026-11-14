-- 0005: the own-footage library (P0.6b.1, ADR 0022). Needs the sqlite-vec extension, which
-- engine.db.connect() loads on every connection.

-- FR-UND-3: vectors in sqlite-vec, sizes per ADR 0006. text_vec rows are keyed by
-- transcript_chunks.id (bge-small-en-v1.5, 384), image_vec rows by scenes.id (CLIP
-- ViT-B-32, 512). The model that made a chunk's vector is transcript_chunks.model; a
-- keyframe's is scenes.image_model.
CREATE VIRTUAL TABLE text_vec USING vec0(embedding float[384]);
CREATE VIRTUAL TABLE image_vec USING vec0(embedding float[512]);

ALTER TABLE scenes ADD COLUMN image_model TEXT;

-- Chunks indexed before this slice: the bge ones (384 floats, 1536 bytes) move in; a fake
-- embedder's vectors of another size stay out of the table.
INSERT INTO text_vec (rowid, embedding)
SELECT id, embedding FROM transcript_chunks WHERE length(embedding) = 1536;

-- §8.2: film keyframes get CLIP vectors too, as a fifth indexing step.
ALTER TABLE film_index ADD COLUMN images_at TEXT;

-- FR-ING-1: every file path the ingest has seen, with its size and mtime when hashed, so an
-- unchanged file skips hashing on the next scan. Several paths may hold the same bytes:
-- they share one asset.
CREATE TABLE asset_paths (
    path     TEXT PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id),
    size     INTEGER NOT NULL CHECK (size >= 0),
    mtime_ns INTEGER NOT NULL
);
CREATE INDEX asset_paths_asset ON asset_paths(asset_id);

-- One row per ingested own-footage asset. Each step commits with its time, so a stopped
-- run resumes and a broken file is skipped until --retry-failed. faces counts the sampled
-- frames that showed a face (FR-UND-5), face_first_s where the first one was.
CREATE TABLE asset_index (
    asset_id      INTEGER PRIMARY KEY REFERENCES assets(id),
    has_audio     INTEGER CHECK (has_audio IS NULL OR has_audio IN (0, 1)),
    probed_at     TEXT,
    transcript_at TEXT,
    scenes_at     TEXT,
    chunks_at     TEXT,
    images_at     TEXT,
    people_at     TEXT,
    faces         INTEGER CHECK (faces IS NULL OR faces >= 0),
    face_first_s  REAL,
    error_step    TEXT,
    error         TEXT,
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- §8.3: Abbas's people marks, one row per decision (the latest one counts); the asset's
-- people_release carries the current state.
CREATE TABLE people_marks (
    id        INTEGER PRIMARY KEY,
    asset_id  INTEGER NOT NULL REFERENCES assets(id),
    decision  TEXT NOT NULL CHECK (decision IN ('yes', 'exclude')),
    note      TEXT NOT NULL DEFAULT '',
    marked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX people_marks_asset ON people_marks(asset_id);
