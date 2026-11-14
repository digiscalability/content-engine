-- 0006: stock fetch and the music pool (P0.6b.2, ADR 0023).

-- FR-ING-5, FR-ING-6, FR-SRC-2: one licence record per asset from a library source (a stock
-- API such as Pexels, or a music pool). Search and the rights check accept such an asset only
-- with its row. attribution may be '' when the licence asks for none. provider and
-- provider_id name the item at the API (a Pexels video id), so it is never fetched twice;
-- fetched_on is the local day of the fetch, which the daily cap counts.
CREATE TABLE asset_licences (
    asset_id    INTEGER PRIMARY KEY REFERENCES assets(id),
    licence_url TEXT NOT NULL CHECK (trim(licence_url) <> ''),
    terms       TEXT NOT NULL CHECK (trim(terms) <> ''),
    attribution TEXT NOT NULL,
    creator     TEXT,
    creator_url TEXT,
    page_url    TEXT,
    provider    TEXT,
    provider_id TEXT,
    fetched_on  TEXT,
    recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (provider, provider_id)
);
CREATE INDEX asset_licences_fetched ON asset_licences(provider, fetched_on);

-- FR-ING-6: the music pool. One row per imported audio asset; energy is the look's band
-- (engine/style/look.py energy_band). A row with error set failed to import and waits for
-- --retry-failed.
CREATE TABLE tracks (
    asset_id    INTEGER PRIMARY KEY REFERENCES assets(id),
    title       TEXT NOT NULL,
    artist      TEXT,
    tempo_bpm   REAL CHECK (tempo_bpm IS NULL OR tempo_bpm > 0),
    energy      TEXT CHECK (energy IS NULL OR energy IN ('low', 'medium', 'high')),
    imported_at TEXT,
    error       TEXT,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- FR-ING-5: stock is fetched only for beats the scriptwriter asked for. search_library
-- queues a beat query of a brief whose best match is weak; `ce stock fetch` works the queue.
CREATE TABLE stock_requests (
    id         INTEGER PRIMARY KEY,
    brief_id   INTEGER NOT NULL REFERENCES briefs(id),
    query      TEXT NOT NULL CHECK (trim(query) <> ''),
    format     TEXT NOT NULL CHECK (format IN ('A', 'B', 'C')),
    best_score REAL,
    status     TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'fetched', 'none_found', 'failed')),
    detail     TEXT,
    asked_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    done_at    TEXT,
    UNIQUE (brief_id, query)
);
CREATE INDEX stock_requests_status ON stock_requests(status);
