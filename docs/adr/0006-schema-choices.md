# ADR 0006: Schema choices SRD §11 left open

- Status: accepted, 2026-11-13 (slice P0.2)
- Requirements: SRD §11, §10, §8.3, FR-SRC-1, FR-SRC-2, GR-1, NFR-1

## Context

SRD §11 lists the tables and key columns but leaves the migration mechanism, id types, column names that clash with SQL keywords, and several constraints open.

## Decision

- **Migrations:** plain SQL files `engine/db/migrations/NNNN_name.sql`, numbered from 0001 with no gaps, applied in one transaction each with `PRAGMA user_version` as the version. No migration framework: one file per change is enough for one local database. A database newer than the code is refused, not opened.
- **Connections:** foreign keys on, WAL, `busy_timeout` 5 s, explicit transactions (`isolation_level=None`).
- **Ids:** integers for engine-made rows (assets, briefs, posts…), so `ce reject 17` and `media/inbox/avatar/17.mp4` stay short; text ids for rows mirrored from config (`sources`, `identities`) and for `agent_runs.run_id`.
- **Renamed columns:** `start`/`end` → `start_s`/`end_s` (`END` is an SQL keyword, and the unit is in the name); `qa_results.check` → `check_name` (`CHECK` is reserved); `assets` gains `media_kind`.
- **Posts carry `brief_id`** and a nullable `render_id`: §10's states start at `planned`, before any render exists. One post per brief and channel (UNIQUE). A post past `avatar_ready` must have a render.
- **Rights in the database (GR-1):** `rights_type` NOT NULL with a CHECK on the seven tiers, `rights_proof` NOT NULL and non-blank; `stock`, `cc_by` and `partner_written` rows need a licence (sources: `licence_url`; assets: `licence_url` and `attribution`). Triggers keep an asset's tier equal to its source's, and refuse to change a source's tier once it has assets.
- **People rule (§8.3):** `people_release` ∈ `none`, `unknown`, `yes`, `exclude`, default `unknown`, so an unchecked asset is excluded from B-roll until the face check or Abbas clears it.
- **Scores** are in [0, 1]. **Idempotency (NFR-1):** metrics key on `(post_id, at)`; ledger rows carry an optional UNIQUE `charge_key` so a resumed run cannot record a charge twice.
- **No `ON DELETE CASCADE`:** deleting a referenced row fails, matching "no tool deletes data" (§5.4).
- **sqlite-vec tables (`text_vec`, `image_vec`) are deferred to P0.6b**, the slice that loads the extension and fixes the vector sizes (384 for `bge-small-en-v1.5`, 512 for CLIP ViT-B-32, unless C12 swaps a model).

## Consequences

- Later slices add a numbered file; editing an applied migration is never done.
- Code that writes rows gets a database error, not silent bad data, when it breaks a rights or state rule; tests for those rules live in `tests/unit/test_db_schema.py`.
