"""Agent read and submit tools (SRD §5.4) against a real DB and the repo's config."""

import json
from datetime import date

import pytest

from engine.agents.state import sync_identities
from engine.agents.tools import TOOLS, ToolContext, call_tool, rights_violations
from engine.config import load_config
from engine.db import open_db
from engine.produce.editplan import EditPlan

TODAY = date(2026, 11, 14)
BRIEF = {
    "identity_id": "selfhelp",
    "format": "C",
    "topic": "focus",
    "angle": "craft",
    "hook": "One habit",
    "cta": "follow",
    "difference_note": "First focus post; list structure",
    "exploration": False,
}


@pytest.fixture
def ctx(tmp_path, repo_root):
    config = load_config(repo_root / "config")
    conn = open_db(tmp_path / "engine.db")
    sync_identities(conn, config)
    return ToolContext(conn=conn, config=config, config_dir=repo_root / "config", today=TODAY)


def source(ctx, sid, tier, kind="folder", **extra):
    cols = {
        "id": sid,
        "kind": kind,
        "path": "C:/x",
        "rights_type": tier,
        "rights_proof": "proof",
        **extra,
    }
    if tier in ("stock", "cc_by", "partner_written"):
        cols.setdefault("licence_url", "https://licence")
    ctx.conn.execute(
        f"INSERT INTO sources ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        list(cols.values()),
    )


def asset(ctx, sid, tier, sha, people="none", media="video", expires=None, duration=20.0) -> int:
    licence = (
        ("https://lic", "credit") if tier in ("stock", "cc_by", "partner_written") else (None, None)
    )
    cur = ctx.conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, duration, rights_type, "
        "people_release, licence_url, attribution, licence_expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, sha * 64, f"C:/media/{sha}.mp4", media, duration, tier, people, *licence, expires),
    )
    return cur.lastrowid


def plan(shots, voice=(), duration_shots=None) -> dict:
    return {
        "timeline": shots,
        "audio": {"voice": list(voice)},
        "look": {"id": "selfhelp", "version": 1},
    }


def test_tools_have_names_descriptions_and_schemas():
    for name, spec in TOOLS.items():
        assert spec.name == name and spec.description and spec.input_schema["type"] == "object"


def test_get_state_lists_selfhelp_at_l0(ctx):
    state = call_tool(ctx, "get_state", {})
    assert state["ok"]
    (identity,) = state["identities"]
    assert identity["id"] == "selfhelp" and identity["autonomy_level"] == 0
    assert identity["kill_flag"] is False and identity["due"] is True


def test_killed_identity_is_not_due(ctx):
    ctx.conn.execute("UPDATE identities SET kill_flag = 1 WHERE id = 'selfhelp'")
    assert call_tool(ctx, "get_state", {})["identities"][0]["due"] is False


def test_sync_keeps_the_kill_flag(ctx):
    ctx.conn.execute("UPDATE identities SET kill_flag = 1 WHERE id = 'selfhelp'")
    sync_identities(ctx.conn, ctx.config)
    assert ctx.conn.execute("SELECT kill_flag FROM identities").fetchone()[0] == 1


def test_get_playbook_returns_the_rules(ctx):
    result = call_tool(ctx, "get_playbook", {"identity": "selfhelp"})
    assert "## Rules (Abbas-owned)" in result["playbook"]


def test_unknown_identity_is_an_error(ctx):
    result = call_tool(ctx, "get_playbook", {"identity": "nobody"})
    assert result["ok"] is False and "nobody" in result["errors"][0]


def test_budget_status_reads_the_ledger_and_caps(ctx):
    ctx.conn.execute(
        "INSERT INTO budget_ledger (vendor, month, units, charge_key) "
        "VALUES ('elevenlabs', '2026-11', 700, 'k')"
    )
    status = call_tool(ctx, "budget_status", {})
    assert status["elevenlabs_chars_month"] == 700
    assert status["elevenlabs_cap"] == 78600 and status["elevenlabs_floor"] == 20000
    assert status["run_budget_usd"] == 3


def test_submit_brief_rejects_a_missing_difference_note(ctx):
    result = call_tool(ctx, "submit_brief", {**BRIEF, "difference_note": " "})
    assert result["ok"] is False and "difference_note" in json.dumps(result["errors"])
    assert ctx.conn.execute("SELECT COUNT(*) FROM briefs").fetchone()[0] == 0


def test_submit_brief_stores_it(ctx):
    result = call_tool(ctx, "submit_brief", BRIEF)
    row = ctx.conn.execute("SELECT * FROM briefs WHERE id = ?", (result["brief_id"],)).fetchone()
    assert row["difference_note"] == BRIEF["difference_note"] and row["format"] == "C"


def test_submit_brief_refuses_a_format_the_identity_does_not_use(ctx):
    result = call_tool(ctx, "submit_brief", {**BRIEF, "format": "A"})
    assert result["ok"] is False and "format A" in result["errors"][0]


def test_submit_script_checks_format_length(ctx):
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    short = {"brief_id": brief_id, "beats": [{"text": "Too short.", "target_s": 5}]}
    assert "30-60 s" in call_tool(ctx, "submit_script", short)["errors"][0]
    beats = [{"text": "Discipline is a skill you can practise.", "target_s": 5}] * 8
    result = call_tool(ctx, "submit_script", {"brief_id": brief_id, "beats": beats})
    assert result["ok"] and result["version"] == 1 and result["duration"] == 40
    assert call_tool(ctx, "submit_script", {"brief_id": brief_id, "beats": beats})["version"] == 2


def test_submit_edit_plan_checks_rights_and_length(ctx):
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    source(ctx, "own", "own")
    good = asset(ctx, "own", "own", "a")
    shots = [
        {"asset_id": good, "in": 0, "out": 15, "role": "avatar"},
        {"asset_id": good, "in": 0, "out": 15, "role": "broll"},
    ]
    ok = call_tool(ctx, "submit_edit_plan", {"brief_id": brief_id, "plan": plan(shots)})
    assert ok["ok"] and ok["duration"] == 30
    too_short = call_tool(ctx, "submit_edit_plan", {"brief_id": brief_id, "plan": plan(shots[:1])})
    assert "30-60 s" in too_short["errors"][0]


def test_search_library_filters(ctx):
    source(ctx, "own", "own")
    source(ctx, "pexels", "stock", kind="library")
    source(ctx, "films", "film_reference", kind="drive", volume_serial="1", volume_label="F")
    clean = asset(ctx, "own", "own", "a")
    asset(ctx, "own", "own", "b", people="unknown")
    asset(ctx, "pexels", "stock", "c", expires="2026-11-01")
    film = asset(ctx, "films", "film_reference", "d")
    found = {
        r["asset_id"]
        for r in call_tool(ctx, "search_library", {"query": "", "format": "C"})["results"]
    }
    assert found == {clean}
    found_a = {
        r["asset_id"]
        for r in call_tool(ctx, "search_library", {"query": "", "format": "A"})["results"]
    }
    assert found_a == {clean, film}


def test_rights_violations_cover_tiers_people_licences_and_film_limits(ctx):
    source(ctx, "own", "own")
    source(ctx, "films", "film_reference", kind="drive", volume_serial="1", volume_label="F")
    own = asset(ctx, "own", "own", "a")
    face = asset(ctx, "own", "own", "b", people="unknown")
    film = asset(ctx, "films", "film_reference", "c", duration=600)

    def check(shots, fmt="C"):
        return " | ".join(
            rights_violations(ctx.conn, EditPlan.model_validate(plan(shots)), fmt, TODAY)
        )

    assert check([{"asset_id": own, "in": 0, "out": 30, "role": "avatar"}]) == ""
    assert "film_reference" in check(
        [
            {"asset_id": film, "in": 0, "out": 5, "role": "evidence"},
            {"asset_id": own, "in": 0, "out": 25, "role": "avatar"},
        ]
    )
    assert "people" in check([{"asset_id": face, "in": 0, "out": 30, "role": "broll"}])
    assert "no such asset" in check([{"asset_id": 999, "in": 0, "out": 30, "role": "broll"}])
    long_cut = check(
        [
            {"asset_id": film, "in": 0, "out": 12, "role": "evidence"},
            {"asset_id": own, "in": 0, "out": 28, "role": "avatar"},
        ],
        "A",
    )
    assert "10 s" in long_cut
    as_broll = check(
        [
            {"asset_id": film, "in": 0, "out": 5, "role": "broll"},
            {"asset_id": own, "in": 0, "out": 25, "role": "avatar"},
        ],
        "A",
    )
    assert "B-roll" in as_broll
    too_much = check(
        [
            {"asset_id": film, "in": 0, "out": 9, "role": "evidence"},
            {"asset_id": film, "in": 20, "out": 29, "role": "evidence"},
            {"asset_id": own, "in": 0, "out": 20, "role": "avatar"},
        ],
        "A",
    )
    assert "30%" in too_much


def test_submit_moment_scores_records_the_scorer(ctx):
    source(ctx, "own", "own")
    a = asset(ctx, "own", "own", "a")
    result = call_tool(
        ctx,
        "submit_moment_scores",
        {
            "scorer_version": "moment-scorer@opus-5-5",
            "scores": [
                {
                    "asset_id": a,
                    "start_s": 1,
                    "end_s": 4,
                    "text": "line",
                    "hook_score": 0.8,
                    "standalone_score": 0.6,
                }
            ],
        },
    )
    row = ctx.conn.execute(
        "SELECT * FROM moments WHERE id = ?", (result["moment_ids"][0],)
    ).fetchone()
    assert row["scorer_version"] == "moment-scorer@opus-5-5" and row["hook_score"] == 0.8


def test_submit_qa_review_needs_a_render(ctx):
    result = call_tool(
        ctx, "submit_qa_review", {"render_id": 5, "verdict": "hold", "reasons": ["x"]}
    )
    assert result["ok"] is False and "render 5" in result["errors"][0]


def test_film_limits_count_on_screen_time(ctx):
    source(ctx, "own", "own")
    source(ctx, "films", "film_reference", kind="drive", volume_serial="1", volume_label="F")
    own = asset(ctx, "own", "own", "a")
    film = asset(ctx, "films", "film_reference", "c", duration=600)
    slowed = [
        {"asset_id": film, "in": 0, "out": 8, "role": "evidence", "ops": {"speed": 0.5}},
        {"asset_id": own, "in": 0, "out": 34, "role": "avatar"},
    ]
    found = " | ".join(
        rights_violations(ctx.conn, EditPlan.model_validate(plan(slowed)), "A", TODAY)
    )
    assert "10 s" in found  # 8 s of source plays for 16 s
    frozen = [
        {
            "asset_id": film,
            "in": 0,
            "out": 9,
            "role": "evidence",
            "ops": {"freeze": {"at": 1, "duration": 5}},
        },
        {"asset_id": film, "in": 30, "out": 39, "role": "evidence"},
        {"asset_id": own, "in": 0, "out": 20, "role": "avatar"},
    ]
    found = " | ".join(
        rights_violations(ctx.conn, EditPlan.model_validate(plan(frozen)), "A", TODAY)
    )
    assert "30%" in found


def test_film_is_never_an_audio_source(ctx):
    source(ctx, "own", "own")
    source(ctx, "films", "film_reference", kind="drive", volume_serial="1", volume_label="F")
    own = asset(ctx, "own", "own", "a")
    film_audio = asset(ctx, "films", "film_reference", "e", media="audio")
    shots = [{"asset_id": own, "in": 0, "out": 40, "role": "avatar"}]
    edit = plan(shots, voice=[{"asset_id": film_audio, "at": 0, "out": 5}])
    found = " | ".join(rights_violations(ctx.conn, EditPlan.model_validate(edit), "A", TODAY))
    assert "audio" in found and "film" in found


def test_a_killed_identity_takes_no_submissions(ctx):
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    ctx.conn.execute("UPDATE identities SET kill_flag = 1 WHERE id = 'selfhelp'")
    assert "kill" in call_tool(ctx, "submit_brief", BRIEF)["errors"][0]
    beats = [{"text": "Discipline is a skill you can practise.", "target_s": 5}] * 8
    assert (
        "kill"
        in call_tool(ctx, "submit_script", {"brief_id": brief_id, "beats": beats})["errors"][0]
    )


# Post rows (§10) ----------------------------------------------------------------------------

BEATS = [{"text": "Discipline is a skill you can practise.", "target_s": 5}] * 8


def post_states(ctx) -> list[tuple[str, str, int]]:
    rows = ctx.conn.execute(
        "SELECT c.platform, p.state, p.ai_disclosure FROM posts p "
        "JOIN channels c ON c.id = p.channel_id ORDER BY p.id"
    )
    return [tuple(r) for r in rows]


def test_submit_brief_plans_one_post_per_channel(ctx):
    call_tool(ctx, "submit_brief", BRIEF)
    assert post_states(ctx) == [("youtube", "planned", 1)]  # format C is an avatar post


def test_submit_script_marks_the_posts_scripted(ctx):
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    call_tool(ctx, "submit_script", {"brief_id": brief_id, "beats": BEATS})
    assert post_states(ctx) == [("youtube", "scripted", 1)]


def test_posts_only_move_forward(ctx):
    from engine.agents.state import advance_posts

    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    advance_posts(ctx.conn, brief_id, "voiced")
    advance_posts(ctx.conn, brief_id, "scripted")  # a second script version after voicing
    assert post_states(ctx)[0][1] == "voiced"
    ctx.conn.execute("UPDATE posts SET state = 'failed'")
    advance_posts(ctx.conn, brief_id, "avatar_ready")
    assert post_states(ctx)[0][1] == "failed"


def test_failing_a_brief_leaves_rendered_and_later_posts_alone(ctx):
    from engine.agents.state import fail_posts

    source(ctx, "own", "own")
    shot = {"asset_id": asset(ctx, "own", "own", "a"), "in": 0, "out": 30, "role": "avatar"}
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    submission = {"brief_id": brief_id, "plan": plan([shot])}
    plan_id = call_tool(ctx, "submit_edit_plan", submission)["edit_plan_id"]
    render_id = ctx.conn.execute(
        "INSERT INTO renders (edit_plan_id, path) VALUES (?, 'r.mp4')", (plan_id,)
    ).lastrowid
    ctx.conn.execute("UPDATE posts SET state = 'drafted', render_id = ?", (render_id,))
    fail_posts(ctx.conn, brief_id, "avatar expired")  # a late expiry of an old request
    assert post_states(ctx)[0][1] == "drafted"
    ctx.conn.execute("UPDATE posts SET state = 'avatar_waiting', render_id = NULL")
    fail_posts(ctx.conn, brief_id, "avatar expired")
    assert post_states(ctx)[0][1] == "failed"


def test_qa_outcomes_are_final(ctx):
    from engine.agents.state import advance_posts

    source(ctx, "own", "own")
    shot = {"asset_id": asset(ctx, "own", "own", "a"), "in": 0, "out": 30, "role": "avatar"}
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    plan_id = call_tool(ctx, "submit_edit_plan", {"brief_id": brief_id, "plan": plan([shot])})[
        "edit_plan_id"
    ]
    render_id = ctx.conn.execute(
        "INSERT INTO renders (edit_plan_id, path) VALUES (?, 'r.mp4')", (plan_id,)
    ).lastrowid
    ctx.conn.execute("UPDATE posts SET state = 'rendered', render_id = ?", (render_id,))
    advance_posts(ctx.conn, brief_id, "rendered", render_id=render_id)  # same state: a no-op
    assert post_states(ctx)[0][1] == "rendered"
    for blocked in ("qa_held", "qa_failed"):
        ctx.conn.execute("UPDATE posts SET state = ?", (blocked,))
        advance_posts(ctx.conn, brief_id, "qa_passed")
        assert post_states(ctx)[0][1] == blocked


def test_get_playbook_lists_the_never_terms_from_the_fact_sheet(ctx, tmp_path):
    from dataclasses import replace

    sheet = tmp_path / "sheet.md"
    sheet.write_text('## Who\n- Never use "guru".\n', encoding="utf-8")
    qa = ctx.config.engine.qa.model_copy(update={"fact_sheet": str(sheet)})
    ctx.config = replace(ctx.config, engine=ctx.config.engine.model_copy(update={"qa": qa}))
    result = call_tool(ctx, "get_playbook", {"identity": "selfhelp"})
    assert result["never_terms"] == ["guru"]  # avoid them before voicing, not only at QA
