"""The SRD §11 schema and the constraints that guard it (P0.2 done-when)."""

import sqlite3

import pytest

from engine.db import open_db

SHA = "a" * 64

SECTION_11_TABLES = {
    "sources",
    "assets",
    "transcripts",
    "scenes",
    "moments",
    "style_profiles",
    "looks",
    "identities",
    "channels",
    "trends",
    "briefs",
    "scripts",
    "edit_plans",
    "renders",
    "qa_results",
    "posts",
    "metrics_snapshots",
    "playbook_changes",
    "budget_ledger",
    "agent_runs",
    "tool_calls",
}


@pytest.fixture
def db(tmp_path) -> sqlite3.Connection:
    return open_db(tmp_path / "engine.db")


def add_source(db, id="own-pics", rights_type="own", proof="Filmed by Abbas", **extra):
    cols = {
        "id": id,
        "kind": "folder",
        "path": "C:/pics",
        "rights_type": rights_type,
        "rights_proof": proof,
        **extra,
    }
    insert(db, "sources", cols)
    return id


def add_asset(db, source_id="own-pics", sha=SHA, rights_type="own", **extra) -> int:
    cols = {
        "source_id": source_id,
        "sha256": sha,
        "path": "C:/pics/a.mp4",
        "media_kind": "video",
        "rights_type": rights_type,
        **extra,
    }
    return insert(db, "assets", cols)


def add_identity_channel(db) -> int:
    insert(db, "identities", {"id": "selfhelp", "display_name": "Self help"})
    return insert(db, "channels", {"identity_id": "selfhelp", "platform": "youtube"})


def add_brief(db, **extra) -> int:
    cols = {
        "identity_id": "selfhelp",
        "format": "C",
        "topic": "focus",
        "angle": "craft",
        "hook": "One habit",
        "cta": "follow",
        "difference_note": "New structure",
        "exploration": 0,
        **extra,
    }
    return insert(db, "briefs", cols)


def add_post(db, brief_id, channel_id, **extra) -> int:
    cols = {"brief_id": brief_id, "channel_id": channel_id, "ai_disclosure": 1, **extra}
    return insert(db, "posts", cols)


def insert(db, table, cols) -> int:
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    cur = db.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(cols.values()))
    return cur.lastrowid


def test_all_section_11_tables_exist(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert names >= SECTION_11_TABLES


@pytest.mark.parametrize(
    "overrides",
    [
        {"rights_type": None},
        {"proof": None},
        {"proof": "   "},
        {"rights_type": "borrowed"},
    ],
)
def test_source_requires_rights_type_and_proof(db, overrides):
    with pytest.raises(sqlite3.IntegrityError):
        add_source(db, **overrides)


def test_duplicate_sha256_rejected(db):
    add_source(db)
    add_asset(db)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        add_asset(db)


def test_asset_rights_must_match_source(db):
    add_source(db)
    with pytest.raises(sqlite3.IntegrityError, match="rights_type"):
        add_asset(db, rights_type="stock", licence_url="https://x", attribution="x")
    asset = add_asset(db)
    with pytest.raises(sqlite3.IntegrityError, match="rights_type"):
        db.execute("UPDATE assets SET rights_type = 'public_domain' WHERE id = ?", (asset,))


def test_stock_asset_needs_licence(db):
    add_source(db, id="pexels", rights_type="stock", licence_url="https://www.pexels.com/license/")
    with pytest.raises(sqlite3.IntegrityError):
        add_asset(db, source_id="pexels", rights_type="stock")
    add_asset(
        db,
        source_id="pexels",
        rights_type="stock",
        licence_url="https://www.pexels.com/video/1/",
        attribution="Video by A. Person",
    )


def test_people_release_defaults_unknown(db):
    add_source(db)
    asset = add_asset(db)
    row = db.execute("SELECT people_release FROM assets WHERE id = ?", (asset,)).fetchone()
    assert row["people_release"] == "unknown"
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE assets SET people_release = 'maybe' WHERE id = ?", (asset,))


def test_asset_needs_existing_source(db):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        add_asset(db, source_id="nowhere")


def test_source_with_assets_cannot_be_deleted(db):
    add_source(db)
    add_asset(db)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.execute("DELETE FROM sources WHERE id = 'own-pics'")


def test_moment_requires_scores_and_scorer_version(db):
    add_source(db)
    asset = add_asset(db)
    base = {
        "asset_id": asset,
        "start_s": 0.0,
        "end_s": 4.0,
        "text": "a line",
        "hook_score": 0.7,
        "standalone_score": 0.6,
        "scorer_version": "moment-scorer@1",
    }
    insert(db, "moments", base)
    for missing in ("hook_score", "standalone_score", "scorer_version"):
        with pytest.raises(sqlite3.IntegrityError):
            insert(db, "moments", {**base, missing: None})
    with pytest.raises(sqlite3.IntegrityError):
        insert(db, "moments", {**base, "hook_score": 1.5})
    with pytest.raises(sqlite3.IntegrityError):
        insert(db, "moments", {**base, "end_s": 0.0})


def test_brief_requires_difference_note(db):
    add_identity_channel(db)
    with pytest.raises(sqlite3.IntegrityError):
        add_brief(db, difference_note="  ")
    with pytest.raises(sqlite3.IntegrityError):
        add_brief(db, format="D")
    add_brief(db)


def test_post_state_must_be_known(db):
    channel = add_identity_channel(db)
    with pytest.raises(sqlite3.IntegrityError):
        add_post(db, add_brief(db), channel, state="posted")


def test_rendered_post_needs_render(db):
    channel = add_identity_channel(db)
    post = add_post(db, add_brief(db), channel)
    assert db.execute("SELECT state FROM posts WHERE id = ?", (post,)).fetchone()[0] == "planned"
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE posts SET state = 'rendered' WHERE id = ?", (post,))


def test_one_post_per_brief_and_channel(db):
    channel = add_identity_channel(db)
    brief = add_brief(db)
    add_post(db, brief, channel)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        add_post(db, brief, channel)


def test_metrics_snapshot_unique_per_window(db):
    channel = add_identity_channel(db)
    post = add_post(db, add_brief(db), channel)
    insert(db, "metrics_snapshots", {"post_id": post, "at": "24h", "views": 10})
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(db, "metrics_snapshots", {"post_id": post, "at": "24h", "views": 11})
    with pytest.raises(sqlite3.IntegrityError):
        insert(db, "metrics_snapshots", {"post_id": post, "at": "48h", "views": 11})


def test_trend_requires_source_query_date_score(db):
    add_identity_channel(db)
    base = {
        "source": "youtube_search",
        "query": "discipline",
        "identity_id": "selfhelp",
        "score": 0.4,
        "date": "2026-11-13",
    }
    insert(db, "trends", base)
    for missing in ("source", "query", "score", "date"):
        with pytest.raises(sqlite3.IntegrityError):
            insert(db, "trends", {**base, missing: None})


def test_json_columns_reject_invalid_json(db):
    add_identity_channel(db)
    brief = add_brief(db)
    with pytest.raises(sqlite3.IntegrityError):
        insert(db, "scripts", {"brief_id": brief, "version": 1, "body": "{not json"})
    insert(db, "scripts", {"brief_id": brief, "version": 1, "body": '{"beats": []}'})
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(db, "scripts", {"brief_id": brief, "version": 1, "body": "{}"})


def test_ledger_charge_key_unique(db):
    row = {"vendor": "elevenlabs", "month": "2026-11", "units": 700, "charge_key": "voice:script:1"}
    insert(db, "budget_ledger", row)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        insert(db, "budget_ledger", row)
    with pytest.raises(sqlite3.IntegrityError):
        insert(db, "budget_ledger", {**row, "charge_key": "k2", "month": "Nov 2026"})
    with pytest.raises(sqlite3.IntegrityError):
        insert(db, "budget_ledger", {**row, "charge_key": "k3", "vendor": "openai"})


def test_autonomy_level_is_0_or_1(db):
    with pytest.raises(sqlite3.IntegrityError):
        insert(db, "identities", {"id": "x", "autonomy_level": 2})


def test_tool_call_needs_its_run(db):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        insert(
            db,
            "tool_calls",
            {
                "run_id": "nope",
                "tool": "mcp__engine__get_state",
                "input_digest": "d",
                "status": "ok",
            },
        )


def test_source_rights_type_frozen_once_assets_exist(db):
    add_source(db)
    db.execute("UPDATE sources SET rights_type = 'public_domain' WHERE id = 'own-pics'")
    db.execute("UPDATE sources SET rights_type = 'own' WHERE id = 'own-pics'")
    add_asset(db)
    with pytest.raises(sqlite3.IntegrityError, match="cannot change"):
        db.execute("UPDATE sources SET rights_type = 'public_domain' WHERE id = 'own-pics'")
