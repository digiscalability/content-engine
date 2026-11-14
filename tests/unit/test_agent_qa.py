"""The QA gate as tools (SRD §6.7): run_qa_checks (FR-QA-1, 5, 6, 8), the gated
submit_qa_review (FR-QA-7) and the copy check in create_draft (FR-QA-8)."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from engine.agents.context import Providers, ToolContext
from engine.agents.dryrun import dry_run_config
from engine.agents.state import advance_posts, start_run, sync_identities
from engine.agents.tools import call_tool
from engine.config import load_config
from engine.db import open_db
from engine.library.embeddings import FakeEmbedder
from engine.library.transcribe import ScriptAlignedTranscriber
from engine.produce.avatar import FakeAvatar
from engine.produce.voice import FakeVoice
from engine.publish.postiz import FakePostiz

TODAY = date(2026, 11, 14)
BRIEF = {
    "identity_id": "selfhelp",
    "format": "C",
    "topic": "starting",
    "angle": "craft",
    "hook": "Start smaller",
    "cta": "follow",
    "difference_note": "First post on starting",
}
CLEAN = [{"text": "Start smaller than feels serious, then keep going.", "target_s": 5}] * 7
COPY = {"platform": "youtube", "title": "Start smaller", "caption": "One line a day.",
        "hashtags": ["#discipline"]}  # fmt: skip
CHECKS = {"rights", "disclosure", "banned_topics", "claims_about_abbas", "self_dedupe", "spec"}
MEASURED = {  # what render_post measures, within every spec limit (FR-QA-4)
    "duration": 35.0,
    "plan_duration": 35.0,
    "video": {"width": 1080, "height": 1920, "fps": 30.0},
    "loudness": {"integrated_lufs": -14.1, "true_peak_dbtp": -1.5},
    "captions": {"coverage": 1.0, "box": [170, 1183, 905, 1275]},
}


def with_fact_sheet(config, path):
    qa = config.engine.qa.model_copy(update={"fact_sheet": str(path)})
    return replace(config, engine=config.engine.model_copy(update={"qa": qa}))


def with_channels(config, *platforms):
    selfhelp = config.identities["selfhelp"]
    channels = [
        selfhelp.channels[0].model_copy(update={"platform": p, "postiz_integration_id": f"{p}-1"})
        for p in platforms
    ]
    identities = {"selfhelp": selfhelp.model_copy(update={"channels": channels})}
    return replace(config, identities=identities)


def make_ctx(tmp_path, config) -> ToolContext:
    conn = open_db(tmp_path / "engine.db")
    sync_identities(conn, config)
    start_run(conn, "run-1", "test")
    providers = Providers(
        voice=FakeVoice(),
        avatar=FakeAvatar(inbox=tmp_path / "inbox"),
        publisher=FakePostiz(),
        transcriber=ScriptAlignedTranscriber,
        overlay=None,
        embedder=FakeEmbedder(),
    )
    return ToolContext(
        conn=conn, config=config, run_id="run-1", today=TODAY, providers=providers,
        media_root=tmp_path / "media", now=lambda: datetime(2026, 11, 14, 4, tzinfo=UTC),
    )  # fmt: skip


@pytest.fixture
def config(repo_root, tmp_path):
    sheet = tmp_path / "Public Fact Sheet.md"
    sheet.write_text("Abbas Ali, software engineer and former chef, Melbourne.", "utf-8")
    return with_fact_sheet(dry_run_config(load_config(repo_root / "config")), sheet)


@pytest.fixture
def ctx(tmp_path, config):
    return make_ctx(tmp_path, config)


def rendered_post(ctx, beats=CLEAN) -> int:
    """A brief, script, edit plan and render row, with the render folder's words.json."""
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    script = call_tool(ctx, "submit_script", {"brief_id": brief_id, "beats": beats})
    asset = ctx.conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, duration, rights_type, "
        "people_release) VALUES ('engine', ?, 'avatar.mp4', 'video', 40, 'ai_generated', 'none')",
        (f"{brief_id:064d}",),
    ).lastrowid
    plan = {
        "timeline": [{"asset_id": asset, "in": 0, "out": 35, "role": "avatar"}],
        "captions": {"block": "captions"},
        "look": {"id": "selfhelp", "version": 1},
    }
    submission = {"brief_id": brief_id, "plan": plan}
    plan_id = call_tool(ctx, "submit_edit_plan", submission)["edit_plan_id"]
    folder = ctx.renders_dir / f"selfhelp-{brief_id}-{plan_id}"
    folder.mkdir(parents=True)
    (folder / "post.mp4").write_bytes(b"mp4")
    words = [{"text": w, "start": 0.1, "end": 0.2} for b in beats for w in b["text"].split()]
    (folder / "words.json").write_text(json.dumps(words), "utf-8")
    report = json.dumps({"scripts": [script["script_id"]], **MEASURED})  # the voiced script
    render_id = ctx.conn.execute(
        "INSERT INTO renders (edit_plan_id, path, report) VALUES (?, ?, ?)",
        (plan_id, str(folder / "post.mp4"), report),
    ).lastrowid
    advance_posts(ctx.conn, brief_id, "rendered", render_id=render_id)
    return render_id


def states(ctx) -> list[str]:
    return [r[0] for r in ctx.conn.execute("SELECT state FROM posts ORDER BY id")]


def checks(ctx, render_id) -> dict[str, str]:
    rows = ctx.conn.execute(
        "SELECT check_name, result FROM qa_results WHERE render_id = ?", (render_id,)
    )
    return {r["check_name"]: r["result"] for r in rows}


def test_a_clean_post_passes_every_check(ctx):
    render_id = rendered_post(ctx)
    result = call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    assert result["ok"] and result["passed"] is True
    assert checks(ctx, render_id) == dict.fromkeys(CHECKS, "pass")
    assert states(ctx) == ["rendered"]  # waiting for the qa-reviewer


def test_a_first_person_script_fails_the_posts(ctx):
    beats = [{"text": "When I worked at a restaurant in Sydney, we started small.",
              "target_s": 5}] * 7  # fmt: skip
    render_id = rendered_post(ctx, beats)
    result = call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    assert result["passed"] is False
    assert checks(ctx, render_id)["claims_about_abbas"] == "fail"
    assert states(ctx) == ["qa_failed"]


def test_an_unchecked_claim_holds_the_posts(tmp_path, config):
    ctx = make_ctx(tmp_path, with_fact_sheet(config, tmp_path / "missing.md"))
    beats = [{"text": "Abbas, a former chef, keeps the first step small.", "target_s": 5}] * 7
    render_id = rendered_post(ctx, beats)
    result = call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    assert result["passed"] is False and checks(ctx, render_id)["claims_about_abbas"] == "hold"
    assert states(ctx) == ["qa_held"]


def test_an_undisclosed_avatar_post_fails(ctx):
    render_id = rendered_post(ctx)
    ctx.conn.execute("UPDATE posts SET ai_disclosure = 0")
    call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    assert checks(ctx, render_id)["disclosure"] == "fail"


def test_a_channel_without_a_disclosure_route_fails(tmp_path, config):
    ctx = make_ctx(tmp_path, with_channels(config, "youtube", "instagram"))
    render_id = rendered_post(ctx)
    result = call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    (disclosure,) = [c for c in result["checks"] if c["name"] == "disclosure"]
    assert disclosure["result"] == "fail" and "instagram" in disclosure["reasons"][0]


def test_rights_are_checked_again_on_the_render(ctx):
    render_id = rendered_post(ctx)
    ctx.conn.execute("UPDATE assets SET people_release = 'unknown'")  # changed after planning
    call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    assert checks(ctx, render_id)["rights"] == "fail"


def test_checks_run_once_per_render(ctx):
    render_id = rendered_post(ctx)
    first = call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    second = call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    assert first["checks"] == second["checks"]
    assert ctx.conn.execute("SELECT COUNT(*) FROM qa_results").fetchone()[0] == len(CHECKS)


# The qa-reviewer (FR-QA-7) -------------------------------------------------------------------


def review(ctx, render_id, verdict="pass", reasons=(), adds=("script", "visuals"), fits=True):
    args = {"render_id": render_id, "verdict": verdict, "reasons": list(reasons),
            "adds": list(adds), "fits_identity": fits}  # fmt: skip
    return call_tool(ctx, "submit_qa_review", args)


def test_review_needs_every_check_passed(ctx):
    render_id = rendered_post(ctx)
    early = review(ctx, render_id)
    assert early["ok"] is False and "run_qa_checks" in early["errors"][0]
    ctx.conn.execute("UPDATE posts SET ai_disclosure = 0")
    call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    after_fail = review(ctx, render_id)
    assert after_fail["ok"] is False and "disclosure" in after_fail["errors"][0]
    assert states(ctx) == ["qa_failed"]


@pytest.mark.parametrize(
    ("verdict", "state"), [("pass", "qa_passed"), ("hold", "qa_held"), ("fail", "qa_failed")]
)
def test_the_review_sets_the_post_state(ctx, verdict, state):
    render_id = rendered_post(ctx)
    call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    reasons = [] if verdict == "pass" else ["tone may not fit"]
    assert review(ctx, render_id, verdict, reasons)["ok"]
    assert states(ctx) == [state]


def test_a_render_is_reviewed_once(ctx):
    render_id = rendered_post(ctx)
    call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    review(ctx, render_id, "hold", ["borderline claim"])
    again = review(ctx, render_id, "pass")
    assert again["ok"] is False and "already" in again["errors"][0]
    assert states(ctx) == ["qa_held"]  # holds never pass on their own (FR-QA-7)


# create_draft checks its copy (FR-QA-8 covers titles and hashtags) ---------------------------


def test_create_draft_checks_the_copy(ctx):
    render_id = rendered_post(ctx)
    call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    review(ctx, render_id)
    bad = {**COPY, "title": "How I start small", "hashtags": ["#getrich"]}
    refused = call_tool(ctx, "create_draft", {"render_id": render_id, "posts": [bad]})
    assert refused["ok"] is False
    assert "first person" in refused["errors"][0] and "get rich" in refused["errors"][0]
    assert ctx.providers.publisher.drafts == {}
    drafted = call_tool(ctx, "create_draft", {"render_id": render_id, "posts": [COPY]})
    assert drafted["ok"] and states(ctx) == ["drafted"]


def test_get_render_report_gives_the_reviewer_the_report_script_and_checks(ctx):
    render_id = rendered_post(ctx)
    ctx.conn.execute(
        "UPDATE renders SET report = ? WHERE id = ?",
        (json.dumps({"tiers": {"ai_generated": 35.0}, "scripts": [1]}), render_id),
    )
    call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    view = call_tool(ctx, "get_render_report", {"render_id": render_id})
    assert view["ok"] and view["report"]["tiers"] == {"ai_generated": 35.0}
    assert view["script"][0]["text"] == CLEAN[0]["text"]
    assert view["captions"].startswith("Start smaller")
    assert {c["name"] for c in view["checks"]} == CHECKS


# The gate holds across renders (automated security review of ad477e2) ------------------------


def second_render(ctx, render_id: int) -> int:
    """Another render of the same edit plan; the posts still point at the first."""
    plan_id, path = ctx.conn.execute(
        "SELECT edit_plan_id, path FROM renders WHERE id = ?", (render_id,)
    ).fetchone()
    return ctx.conn.execute(
        "INSERT INTO renders (edit_plan_id, path) VALUES (?, ?)", (plan_id, path)
    ).lastrowid


def test_a_failed_post_never_becomes_passed(ctx):
    first = rendered_post(ctx)
    ctx.conn.execute("UPDATE posts SET ai_disclosure = 0")
    call_tool(ctx, "run_qa_checks", {"render_id": first})
    assert states(ctx) == ["qa_failed"]
    ctx.conn.execute("UPDATE posts SET ai_disclosure = 1")
    second = second_render(ctx, first)
    call_tool(ctx, "run_qa_checks", {"render_id": second})
    review(ctx, second)  # passes, but judges a render the posts are not on
    assert states(ctx) == ["qa_failed"]
    refused = call_tool(ctx, "create_draft", {"render_id": first, "posts": [COPY]})
    assert refused["ok"] is False and ctx.providers.publisher.drafts == {}


def test_create_draft_needs_the_stored_passes(ctx):
    render_id = rendered_post(ctx)
    ctx.conn.execute("UPDATE posts SET state = 'qa_passed'")  # no checks, no review
    refused = call_tool(ctx, "create_draft", {"render_id": render_id, "posts": [COPY]})
    assert refused["ok"] is False and "run_qa_checks" in refused["errors"][0]


# QA checks what was voiced, not the newest script (automated security review) ---------------

FIRST_PERSON = [{"text": "When I worked late, we cut corners and paid for it.", "target_s": 5}] * 7


def test_qa_checks_the_voiced_script_not_a_newer_version(ctx):
    render_id = rendered_post(ctx, FIRST_PERSON)  # the voice and captions say this
    brief_id = ctx.conn.execute("SELECT brief_id FROM posts").fetchone()[0]
    call_tool(ctx, "submit_script", {"brief_id": brief_id, "beats": CLEAN})  # a clean v2 after
    folder = Path(ctx.conn.execute("SELECT path FROM renders").fetchone()[0]).parent
    (folder / "words.json").unlink()  # a plan without captions: only the script tells
    result = call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    assert result["passed"] is False
    assert checks(ctx, render_id)["claims_about_abbas"] == "fail"


def test_qa_refuses_a_render_with_no_record_of_its_script(ctx):
    render_id = rendered_post(ctx)
    ctx.conn.execute("UPDATE renders SET report = NULL")
    result = call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    assert result["ok"] is False and "voiced script" in result["errors"][0]


# Self-dedupe (FR-QA-3) -------------------------------------------------------------------------

OTHER = [
    {"text": "Silence is a tool: wait three seconds longer than feels easy.", "target_s": 5}
] * 7


def passed_and_drafted(ctx, beats=CLEAN) -> int:
    """A post that went through QA and became a draft: it counts for self-dedupe."""
    render_id = rendered_post(ctx, beats)
    assert call_tool(ctx, "run_qa_checks", {"render_id": render_id})["passed"]
    ctx.conn.execute("UPDATE posts SET state = 'drafted' WHERE render_id = ?", (render_id,))
    return render_id


def test_a_copied_script_fails_self_dedupe(ctx):
    passed_and_drafted(ctx)
    copy = rendered_post(ctx)
    result = call_tool(ctx, "run_qa_checks", {"render_id": copy})
    assert result["passed"] is False and checks(ctx, copy)["self_dedupe"] == "fail"
    reason = next(c for c in result["checks"] if c["name"] == "self_dedupe")["reasons"][0]
    assert "brief 1" in reason and "0.90" in reason


def test_a_different_script_passes_self_dedupe(ctx):
    passed_and_drafted(ctx)
    other = rendered_post(ctx, OTHER)
    assert call_tool(ctx, "run_qa_checks", {"render_id": other})["passed"] is True


def test_failed_posts_do_not_count_for_self_dedupe(ctx):
    first = passed_and_drafted(ctx)
    ctx.conn.execute("UPDATE posts SET state = 'qa_failed' WHERE render_id = ?", (first,))
    again = rendered_post(ctx)
    assert call_tool(ctx, "run_qa_checks", {"render_id": again})["passed"] is True


def test_self_dedupe_looks_back_only_so_far(ctx):
    passed_and_drafted(ctx)
    qa = ctx.config.engine.qa
    lookback = qa.self_dedupe.model_copy(update={"lookback_posts": 1})
    ctx.config = replace(
        ctx.config,
        engine=ctx.config.engine.model_copy(
            update={"qa": qa.model_copy(update={"self_dedupe": lookback})}
        ),
    )
    passed_and_drafted(ctx, OTHER)  # the one post inside the window
    late_copy = rendered_post(ctx)
    assert call_tool(ctx, "run_qa_checks", {"render_id": late_copy})["passed"] is True


def test_qa_stores_the_script_embedding(ctx):
    render_id = rendered_post(ctx)
    call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    blob = ctx.conn.execute(
        "SELECT script_embedding FROM renders WHERE id = ?", (render_id,)
    ).fetchone()[0]
    assert blob is not None and len(blob) == FakeEmbedder.dims * 4


def test_a_render_checked_before_p13_gets_only_the_new_checks(ctx):
    render_id = rendered_post(ctx)
    for name in ("rights", "disclosure", "banned_topics", "claims_about_abbas"):
        ctx.conn.execute(
            "INSERT INTO qa_results (render_id, check_name, result, details) "
            "VALUES (?, ?, 'pass', '{}')",
            (render_id, name),
        )
    call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    assert ctx.conn.execute("SELECT COUNT(*) FROM qa_results").fetchone()[0] == len(CHECKS)


@pytest.mark.model
def test_a_reworded_script_fails_self_dedupe(tmp_path, config):
    from engine.library.embeddings import BgeSmall

    ctx = make_ctx(tmp_path, config)
    ctx.providers = replace(ctx.providers, embedder=BgeSmall())
    original = [
        {"text": "Most plans fail before they start, so shrink the first step.", "target_s": 5},
        {"text": "Open the file, write one line, then stop if you want.", "target_s": 5},
        {"text": "Starting is the hard part; the rest follows a small win.", "target_s": 5},
    ] * 3
    reworded = [
        {"text": "Most plans die before they begin, so make the first step tiny.", "target_s": 5},
        {"text": "Open the document, write a single line, and stop if you like.", "target_s": 5},
        {"text": "Starting is the hard bit; the rest follows one small win.", "target_s": 5},
    ] * 3
    passed_and_drafted(ctx, original)
    copy = rendered_post(ctx, reworded)
    call_tool(ctx, "run_qa_checks", {"render_id": copy})
    assert checks(ctx, copy)["self_dedupe"] == "fail"


# Spec (FR-QA-4) ---------------------------------------------------------------------------------


def test_a_render_off_spec_fails_the_posts(ctx):
    render_id = rendered_post(ctx)
    report = json.loads(ctx.conn.execute("SELECT report FROM renders").fetchone()[0])
    report["captions"]["box"] = [103, 1183, 980, 1275]  # under the button rail
    ctx.conn.execute("UPDATE renders SET report = ?", (json.dumps(report),))
    result = call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    assert result["passed"] is False and checks(ctx, render_id)["spec"] == "fail"
    assert states(ctx) == ["qa_failed"]


# The reviewer's checklist (FR-QA-7, GR-3) -------------------------------------------------------


def test_the_review_stores_its_checklist(ctx):
    render_id = rendered_post(ctx)
    call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    assert review(ctx, render_id, adds=("script", "education"))["ok"]
    details = ctx.conn.execute(
        "SELECT details FROM qa_results WHERE check_name = 'qa_review'"
    ).fetchone()[0]
    assert json.loads(details) == {"reasons": [], "adds": ["script", "education"],
                                   "fits_identity": True}  # fmt: skip


def test_a_pass_with_one_addition_is_refused_and_the_post_waits(ctx):
    render_id = rendered_post(ctx)
    call_tool(ctx, "run_qa_checks", {"render_id": render_id})
    refused = review(ctx, render_id, adds=("script",))
    assert refused["ok"] is False and "GR-3" in " ".join(refused["errors"])
    assert states(ctx) == ["rendered"]  # nothing stored: the reviewer can still answer


def test_get_render_report_shows_the_difference_note(ctx):
    render_id = rendered_post(ctx)
    view = call_tool(ctx, "get_render_report", {"render_id": render_id})
    assert view["brief"]["difference_note"] == BRIEF["difference_note"]
