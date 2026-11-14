-- GR-7 and §17: every kill, platform warning and resume of an identity, in order (ADR 0019).
-- The digest reads them, and L1's "zero platform warnings in 30 days" will count 'warn' rows.
CREATE TABLE identity_events (
    id          INTEGER PRIMARY KEY,
    identity_id TEXT NOT NULL REFERENCES identities(id),
    kind        TEXT NOT NULL CHECK (kind IN ('kill', 'warn', 'resume')),
    detail      TEXT NOT NULL CHECK (trim(detail) <> ''),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX identity_events_identity ON identity_events(identity_id, id);
