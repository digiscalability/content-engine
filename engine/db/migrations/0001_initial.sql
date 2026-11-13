-- 0001: the SRD §11 schema. Choices the SRD left open are in docs/adr/0006-schema-choices.md.
-- Constraints live here so a bad row fails at the database whatever code wrote it.
-- No ON DELETE CASCADE anywhere: deleting a referenced row fails (nothing deletes data, §5.4).
-- Times are ISO 8601 UTC text. JSON columns are checked with json_valid.

-- Sources and rights (§8, FR-SRC-1, FR-SRC-2, GR-1) -----------------------------------

CREATE TABLE sources (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('folder', 'drive', 'channel', 'library')),
    path          TEXT,
    volume_serial TEXT,
    volume_label  TEXT,
    url           TEXT,
    rights_type   TEXT NOT NULL CHECK (rights_type IN (
                      'own', 'ai_generated', 'stock', 'partner_written', 'cc_by',
                      'public_domain', 'film_reference')),
    rights_proof  TEXT NOT NULL CHECK (trim(rights_proof) <> ''),
    licence_url   TEXT,
    attribution   TEXT,
    expires_at    TEXT,
    CHECK (rights_type NOT IN ('stock', 'cc_by', 'partner_written')
           OR coalesce(trim(licence_url), '') <> '')
);

CREATE TABLE assets (
    id                 INTEGER PRIMARY KEY,
    source_id          TEXT NOT NULL REFERENCES sources(id),
    sha256             TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
    path               TEXT NOT NULL,
    media_kind         TEXT NOT NULL CHECK (media_kind IN ('video', 'image', 'audio')),
    duration           REAL,
    width              INTEGER,
    height             INTEGER,
    fps                REAL,
    rights_type        TEXT NOT NULL CHECK (rights_type IN (
                           'own', 'ai_generated', 'stock', 'partner_written', 'cc_by',
                           'public_domain', 'film_reference')),
    licence_url        TEXT,
    attribution        TEXT,
    licence_expires_at TEXT,
    -- §8.3: 'none' = no faces but Abbas's; 'unknown' = not checked or unmatched faces
    -- (excluded from B-roll); 'yes' = release held; 'exclude' = never use.
    people_release     TEXT NOT NULL DEFAULT 'unknown'
                           CHECK (people_release IN ('none', 'unknown', 'yes', 'exclude')),
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_used_at       TEXT,
    CHECK (rights_type NOT IN ('stock', 'cc_by', 'partner_written')
           OR (coalesce(trim(licence_url), '') <> '' AND attribution IS NOT NULL))
);
CREATE INDEX assets_source ON assets(source_id);

-- An asset carries its source's tier; a mismatch is a rights error, not a data quirk.
CREATE TRIGGER assets_rights_match_source_insert
BEFORE INSERT ON assets
WHEN EXISTS (SELECT 1 FROM sources s WHERE s.id = NEW.source_id AND s.rights_type <> NEW.rights_type)
BEGIN
    SELECT RAISE(ABORT, 'asset rights_type must match its source');
END;

CREATE TRIGGER assets_rights_match_source_update
BEFORE UPDATE OF rights_type, source_id ON assets
WHEN EXISTS (SELECT 1 FROM sources s WHERE s.id = NEW.source_id AND s.rights_type <> NEW.rights_type)
BEGIN
    SELECT RAISE(ABORT, 'asset rights_type must match its source');
END;

CREATE TRIGGER sources_rights_frozen_with_assets
BEFORE UPDATE OF rights_type ON sources
WHEN NEW.rights_type <> OLD.rights_type
     AND EXISTS (SELECT 1 FROM assets a WHERE a.source_id = OLD.id)
BEGIN
    SELECT RAISE(ABORT, 'source rights_type cannot change while it has assets');
END;

-- Understanding (§6.3) -----------------------------------------------------------------

CREATE TABLE transcripts (
    asset_id   INTEGER NOT NULL REFERENCES assets(id),
    model      TEXT NOT NULL,
    language   TEXT,
    words      TEXT NOT NULL CHECK (json_valid(words)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (asset_id, model)
);

CREATE TABLE scenes (
    id            INTEGER PRIMARY KEY,
    asset_id      INTEGER NOT NULL REFERENCES assets(id),
    start_s       REAL NOT NULL CHECK (start_s >= 0),
    end_s         REAL NOT NULL,
    keyframe_path TEXT,
    CHECK (end_s > start_s)
);
CREATE INDEX scenes_asset ON scenes(asset_id);

CREATE TABLE moments (
    id               INTEGER PRIMARY KEY,
    asset_id         INTEGER NOT NULL REFERENCES assets(id),
    start_s          REAL NOT NULL CHECK (start_s >= 0),
    end_s            REAL NOT NULL,
    text             TEXT,
    hook_score       REAL NOT NULL CHECK (hook_score BETWEEN 0 AND 1),
    standalone_score REAL NOT NULL CHECK (standalone_score BETWEEN 0 AND 1),
    scorer_version   TEXT NOT NULL CHECK (trim(scorer_version) <> ''),
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (end_s > start_s)
);
CREATE INDEX moments_asset ON moments(asset_id);

-- Identities and channels: mirrors of config (§12, §17) -------------------------------

CREATE TABLE identities (
    id             TEXT PRIMARY KEY,
    display_name   TEXT,
    autonomy_level INTEGER NOT NULL DEFAULT 0 CHECK (autonomy_level IN (0, 1)),
    kill_flag      INTEGER NOT NULL DEFAULT 0 CHECK (kill_flag IN (0, 1)),
    kill_reason    TEXT
);

CREATE TABLE channels (
    id                    INTEGER PRIMARY KEY,
    identity_id           TEXT NOT NULL REFERENCES identities(id),
    platform              TEXT NOT NULL CHECK (platform IN ('youtube', 'tiktok', 'instagram')),
    postiz_integration_id TEXT,
    ai_disclosure         TEXT,
    UNIQUE (identity_id, platform)
);

-- Style and looks (§9) -----------------------------------------------------------------

CREATE TABLE style_profiles (
    id          INTEGER PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES identities(id),
    measured_at TEXT NOT NULL,
    metrics     TEXT NOT NULL CHECK (json_valid(metrics)),
    palette     TEXT CHECK (palette IS NULL OR json_valid(palette)),
    ref_count   INTEGER NOT NULL CHECK (ref_count > 0)
);

CREATE TABLE looks (
    identity_id TEXT NOT NULL REFERENCES identities(id),
    version     INTEGER NOT NULL CHECK (version >= 1),
    path        TEXT NOT NULL,
    approved_at TEXT,
    PRIMARY KEY (identity_id, version)
);

-- Trends (FR-TRD-1, Phase 2) -----------------------------------------------------------

CREATE TABLE trends (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    query       TEXT NOT NULL,
    identity_id TEXT NOT NULL REFERENCES identities(id),
    score       REAL NOT NULL,
    date        TEXT NOT NULL
);

-- Plan and produce (§6.5, §6.6, §7.1) --------------------------------------------------

CREATE TABLE briefs (
    id              INTEGER PRIMARY KEY,
    identity_id     TEXT NOT NULL REFERENCES identities(id),
    format          TEXT NOT NULL CHECK (format IN ('A', 'B', 'C')),
    topic           TEXT NOT NULL,
    angle           TEXT NOT NULL,
    hook            TEXT NOT NULL,
    cta             TEXT NOT NULL,
    source_moments  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(source_moments)),
    difference_note TEXT NOT NULL CHECK (trim(difference_note) <> ''),
    exploration     INTEGER NOT NULL DEFAULT 0 CHECK (exploration IN (0, 1)),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE scripts (
    id         INTEGER PRIMARY KEY,
    brief_id   INTEGER NOT NULL REFERENCES briefs(id),
    version    INTEGER NOT NULL CHECK (version >= 1),
    body       TEXT NOT NULL CHECK (json_valid(body)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (brief_id, version)
);

CREATE TABLE edit_plans (
    id         INTEGER PRIMARY KEY,
    brief_id   INTEGER NOT NULL REFERENCES briefs(id),
    version    INTEGER NOT NULL CHECK (version >= 1),
    plan       TEXT NOT NULL CHECK (json_valid(plan)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (brief_id, version)
);

CREATE TABLE renders (
    id               INTEGER PRIMARY KEY,
    edit_plan_id     INTEGER NOT NULL REFERENCES edit_plans(id),
    path             TEXT NOT NULL,
    report           TEXT CHECK (report IS NULL OR json_valid(report)),
    frame_hashes     TEXT CHECK (frame_hashes IS NULL OR json_valid(frame_hashes)),
    script_embedding BLOB,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE qa_results (
    id         INTEGER PRIMARY KEY,
    render_id  INTEGER NOT NULL REFERENCES renders(id),
    check_name TEXT NOT NULL,
    result     TEXT NOT NULL CHECK (result IN ('pass', 'fail', 'hold')),
    details    TEXT CHECK (details IS NULL OR json_valid(details)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX qa_results_render ON qa_results(render_id);

-- Posts (§10) --------------------------------------------------------------------------

CREATE TABLE posts (
    id            INTEGER PRIMARY KEY,
    brief_id      INTEGER NOT NULL REFERENCES briefs(id),
    channel_id    INTEGER NOT NULL REFERENCES channels(id),
    render_id     INTEGER REFERENCES renders(id),
    state         TEXT NOT NULL DEFAULT 'planned' CHECK (state IN (
                      'planned', 'scripted', 'voiced', 'avatar_waiting', 'avatar_ready',
                      'rendered', 'qa_failed', 'qa_held', 'qa_passed', 'drafted',
                      'uploaded_private', 'scheduled', 'published', 'rejected', 'failed')),
    caption       TEXT,
    title         TEXT,
    hashtags      TEXT CHECK (hashtags IS NULL OR json_valid(hashtags)),
    ai_disclosure INTEGER NOT NULL CHECK (ai_disclosure IN (0, 1)),
    postiz_id     TEXT,
    platform_id   TEXT,
    scheduled_at  TEXT,
    published_at  TEXT,
    reject_reason TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at    TEXT,
    UNIQUE (brief_id, channel_id),
    CHECK (render_id IS NOT NULL OR state IN (
        'planned', 'scripted', 'voiced', 'avatar_waiting', 'avatar_ready', 'failed', 'rejected'))
);
CREATE INDEX posts_state ON posts(state);

-- Learn (§6.9) -------------------------------------------------------------------------

CREATE TABLE metrics_snapshots (
    post_id         INTEGER NOT NULL REFERENCES posts(id),
    at              TEXT NOT NULL CHECK (at IN ('24h', '72h', '7d')),
    views           INTEGER,
    watch_time      REAL,
    likes           INTEGER,
    comments        INTEGER,
    shares          INTEGER,
    followers_delta INTEGER,
    clicks          INTEGER,
    taken_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (post_id, at)
);

CREATE TABLE playbook_changes (
    id          INTEGER PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES identities(id),
    diff        TEXT NOT NULL,
    reasons     TEXT NOT NULL,
    applied_at  TEXT
);

-- Runs, tool calls and spend (NFR-3, NFR-4) --------------------------------------------

CREATE TABLE agent_runs (
    run_id      TEXT PRIMARY KEY,
    command     TEXT NOT NULL,
    session_id  TEXT,
    cost_usd    REAL,
    model_usage TEXT CHECK (model_usage IS NULL OR json_valid(model_usage)),
    turns       INTEGER,
    subtype     TEXT,
    report      TEXT CHECK (report IS NULL OR json_valid(report)),
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    ended_at    TEXT
);

CREATE TABLE tool_calls (
    id           INTEGER PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES agent_runs(run_id),
    tool         TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    status       TEXT NOT NULL,
    duration_ms  INTEGER,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX tool_calls_run ON tool_calls(run_id);

CREATE TABLE budget_ledger (
    id         INTEGER PRIMARY KEY,
    vendor     TEXT NOT NULL CHECK (vendor IN ('anthropic', 'elevenlabs', 'heygen')),
    month      TEXT NOT NULL CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    units      REAL NOT NULL,
    cost       REAL,
    run_id     TEXT REFERENCES agent_runs(run_id),
    -- NFR-1: the caller's idempotency key (for example 'voice:script:17'), so a resumed
    -- run cannot record the same charge twice.
    charge_key TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX budget_ledger_vendor_month ON budget_ledger(vendor, month);
