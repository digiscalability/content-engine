-- SRD §8.2 film index (ADR 0020). One row per indexed film: which steps are done and the
-- last error, so a stopped run resumes and a broken file is not retried every night.
CREATE TABLE film_index (
    asset_id      INTEGER PRIMARY KEY REFERENCES assets(id),
    probed_at     TEXT,
    transcript_at TEXT,
    scenes_at     TEXT,
    chunks_at     TEXT,
    error_step    TEXT,
    error         TEXT,
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- FR-UND-3: transcript chunks with their text embeddings, as float32 blobs until the
-- sqlite-vec tables arrive with P0.6b (ADR 0006).
CREATE TABLE transcript_chunks (
    id        INTEGER PRIMARY KEY,
    asset_id  INTEGER NOT NULL REFERENCES assets(id),
    start_s   REAL NOT NULL CHECK (start_s >= 0),
    end_s     REAL NOT NULL,
    text      TEXT NOT NULL CHECK (trim(text) <> ''),
    model     TEXT NOT NULL,
    embedding BLOB NOT NULL,
    CHECK (end_s > start_s)
);
CREATE INDEX transcript_chunks_asset ON transcript_chunks(asset_id);
