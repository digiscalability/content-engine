"""Action tools (SRD §5.4): synthesize_voice, request_avatar, render_post, create_draft, on
the P0.6 fakes and a real DB. The full render runs in the dry-run golden test."""

import json
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from engine.agents.context import Providers, ToolContext
from engine.agents.dryrun import dry_run_config
from engine.agents.state import start_run, sync_identities
from engine.agents.tools import TOOLS, call_tool
from engine.config import load_config
from engine.db import open_db
from engine.library.transcribe import ScriptAlignedTranscriber
from engine.produce.avatar import AvatarStatus, FakeAvatar
from engine.produce.voice import FakeVoice
from engine.publish.postiz import FakePostiz

TODAY = date(2026, 11, 14)
NOW = datetime(2026, 11, 14, 4, 0, tzinfo=UTC)
BRIEF = {
    "identity_id": "selfhelp",
    "format": "C",
    "topic": "starting",
    "angle": "craft",
    "hook": "Start smaller",
    "cta": "follow",
    "difference_note": "First post on starting; question hook",
}
BEATS = [{"text": "Discipline is a skill you can practise.", "target_s": 5}] * 8
PROBE = {
    "streams": [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1080,
            "height": 1920,
            "r_frame_rate": "30/1",
            "pix_fmt": "yuv420p",
        }
    ],
    "format": {"duration": "16.8"},
}


def fake_run(argv, env=None):
    """ffprobe answers PROBE; ffmpeg (the fake avatar) writes its output file."""
    if Path(argv[0]).stem == "ffprobe":
        return subprocess.CompletedProcess(argv, 0, json.dumps(PROBE), "")
    Path(argv[-1]).write_bytes(b"video")
    return subprocess.CompletedProcess(argv, 0, "", "")


class NoOverlay:
    def render(self, composition_dir, output):
        raise AssertionError("no overlay render in unit tests")


@pytest.fixture
def ctx(tmp_path, repo_root):
    config = dry_run_config(load_config(repo_root / "config"))
    conn = open_db(tmp_path / "engine.db")
    sync_identities(conn, config)
    start_run(conn, "run-1", "test")
    providers = Providers(
        voice=FakeVoice(),
        avatar=FakeAvatar(inbox=tmp_path / "inbox", run=fake_run),
        publisher=FakePostiz(),
        transcriber=ScriptAlignedTranscriber,
        overlay=NoOverlay(),
        run=fake_run,
    )
    return ToolContext(
        conn=conn,
        config=config,
        config_dir=repo_root / "config",
        run_id="run-1",
        today=TODAY,
        providers=providers,
        media_root=tmp_path / "media",
        now=lambda: NOW,
    )


def scripted(ctx) -> tuple[int, int]:
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    script_id = call_tool(ctx, "submit_script", {"brief_id": brief_id, "beats": BEATS})["script_id"]
    return brief_id, script_id


def states(ctx) -> list[str]:
    return [r[0] for r in ctx.conn.execute("SELECT state FROM posts ORDER BY id")]


def test_action_tools_are_registered_as_actions():
    for name in ("synthesize_voice", "request_avatar", "render_post", "create_draft"):
        assert TOOLS[name].kind == "action" and TOOLS[name].input_schema["type"] == "object"


def test_action_tools_refuse_without_providers(ctx):
    ctx.providers = None
    _, script_id = scripted(ctx)
    result = call_tool(ctx, "synthesize_voice", {"script_id": script_id})
    assert result["ok"] is False and "providers" in result["errors"][0]


# synthesize_voice ---------------------------------------------------------------------------


def test_synthesize_voice_registers_the_wav_and_marks_posts_voiced(ctx):
    _, script_id = scripted(ctx)
    result = call_tool(ctx, "synthesize_voice", {"script_id": script_id})
    assert result["ok"] and result["characters"] == len(" ".join(b["text"] for b in BEATS))
    asset = ctx.conn.execute("SELECT * FROM assets WHERE id = ?", (result["asset_id"],)).fetchone()
    assert asset["media_kind"] == "audio" and asset["rights_type"] == "ai_generated"
    assert asset["duration"] == result["duration"] == pytest.approx(56 / 2.5)
    assert Path(asset["path"]) == ctx.work_dir / "voice" / f"script-{script_id}.wav"
    assert states(ctx) == ["voiced"]


def test_synthesize_voice_twice_charges_once(ctx):
    _, script_id = scripted(ctx)
    first = call_tool(ctx, "synthesize_voice", {"script_id": script_id})
    second = call_tool(ctx, "synthesize_voice", {"script_id": script_id})
    assert first["asset_id"] == second["asset_id"]
    assert len(ctx.providers.voice.calls) == 1
    ledger = ctx.conn.execute("SELECT charge_key, units FROM budget_ledger").fetchall()
    assert [tuple(r) for r in ledger] == [(f"voice:script:{script_id}", first["characters"])]


def test_synthesize_voice_needs_the_identity_voice_id(ctx, repo_root):
    ctx.config = load_config(repo_root / "config")  # the real config: no clone yet
    _, script_id = scripted(ctx)
    result = call_tool(ctx, "synthesize_voice", {"script_id": script_id})
    assert result["ok"] is False and "G0.4" in result["errors"][0]


def test_synthesize_voice_reports_a_budget_refusal(ctx):
    ctx.providers.voice = FakeVoice(limit=20_100)  # 100 characters above the floor
    _, script_id = scripted(ctx)
    result = call_tool(ctx, "synthesize_voice", {"script_id": script_id})
    assert result["ok"] is False and "account floor" in result["errors"][0]
    assert ctx.conn.execute("SELECT COUNT(*) FROM budget_ledger").fetchone()[0] == 0


# request_avatar -----------------------------------------------------------------------------


def test_request_avatar_needs_the_voice_first(ctx):
    brief_id, script_id = scripted(ctx)
    result = call_tool(ctx, "request_avatar", {"brief_id": brief_id, "script_id": script_id})
    assert result["ok"] is False and "synthesize_voice" in result["errors"][0]


def test_request_avatar_refuses_a_script_of_another_brief(ctx):
    _, script_id = scripted(ctx)
    other_brief, _ = scripted(ctx)
    call_tool(ctx, "synthesize_voice", {"script_id": script_id})
    result = call_tool(ctx, "request_avatar", {"brief_id": other_brief, "script_id": script_id})
    assert result["ok"] is False and "belongs to brief" in result["errors"][0]


def test_request_avatar_ready_registers_the_video(ctx):
    brief_id, script_id = scripted(ctx)
    call_tool(ctx, "synthesize_voice", {"script_id": script_id})
    result = call_tool(ctx, "request_avatar", {"brief_id": brief_id, "script_id": script_id})
    assert result["ok"] and result["state"] == "ready"
    asset = ctx.conn.execute("SELECT * FROM assets WHERE id = ?", (result["asset_id"],)).fetchone()
    assert (asset["media_kind"], asset["width"], asset["height"]) == ("video", 1080, 1920)
    assert asset["people_release"] == "none" and asset["duration"] == 16.8
    assert states(ctx) == ["avatar_ready"]


def test_request_avatar_waiting_returns_no_asset(ctx):
    ctx.providers.avatar = FakeAvatar(inbox=ctx.media_root / "inbox", ready=False, run=fake_run)
    brief_id, script_id = scripted(ctx)
    call_tool(ctx, "synthesize_voice", {"script_id": script_id})
    before = ctx.conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    result = call_tool(ctx, "request_avatar", {"brief_id": brief_id, "script_id": script_id})
    assert result["ok"] and result["state"] == "waiting" and "asset_id" not in result
    assert result["needs_abbas"] and "brief" in result["detail"]
    assert ctx.conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == before
    assert states(ctx) == ["avatar_waiting"]


def test_an_expired_avatar_request_fails_the_posts(ctx):
    class Expired:
        def request(self, req):
            return AvatarStatus("expired", detail="no video after 2 days, 0:00:00")

        poll = request

    ctx.providers.avatar = Expired()
    brief_id, script_id = scripted(ctx)
    call_tool(ctx, "synthesize_voice", {"script_id": script_id})
    result = call_tool(ctx, "request_avatar", {"brief_id": brief_id, "script_id": script_id})
    assert result["ok"] is False and "expired" in result["errors"][0]
    row = ctx.conn.execute("SELECT state, reject_reason FROM posts").fetchone()
    assert row["state"] == "failed" and "expired" in row["reject_reason"]


# render_post --------------------------------------------------------------------------------


def edit_plan(ctx, brief_id: int, **extra) -> int:
    asset_id = ctx.conn.execute("SELECT id FROM assets WHERE media_kind = 'video'").fetchone()[0]
    plan = {
        "timeline": [{"asset_id": asset_id, "in": 0, "out": 30, "role": "avatar"}],
        "look": {"id": "selfhelp", "version": 1},
        **extra,
    }
    return call_tool(ctx, "submit_edit_plan", {"brief_id": brief_id, "plan": plan})["edit_plan_id"]


def test_render_post_refuses_overlay_blocks_until_a_look_exists(ctx):
    brief_id, script_id = scripted(ctx)
    call_tool(ctx, "synthesize_voice", {"script_id": script_id})
    call_tool(ctx, "request_avatar", {"brief_id": brief_id, "script_id": script_id})
    overlays = [{"block": "title", "kind": "title_card", "start": 0, "duration": 2}]
    plan_id = edit_plan(ctx, brief_id, overlays=overlays)
    result = call_tool(ctx, "render_post", {"edit_plan_id": plan_id})
    assert result["ok"] is False and "P0.8" in result["errors"][0]


def test_render_post_refuses_an_unknown_plan(ctx):
    result = call_tool(ctx, "render_post", {"edit_plan_id": 99})
    assert result["ok"] is False and "no edit plan 99" in result["errors"][0]


# create_draft -------------------------------------------------------------------------------

COPY = {
    "platform": "youtube",
    "title": "Start smaller than you think",
    "caption": "The first step should feel almost too easy.",
    "hashtags": ["#discipline", "#habits"],
}


def rendered(ctx, state: str = "rendered") -> int:
    brief_id, _ = scripted(ctx)
    asset = ctx.conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, rights_type, people_release) "
        "VALUES ('engine', ?, 'a.mp4', 'video', 'ai_generated', 'none')",
        (f"{brief_id:064d}",),
    ).lastrowid
    plan = {
        "timeline": [{"asset_id": asset, "in": 0, "out": 30, "role": "avatar"}],
        "look": {"id": "selfhelp", "version": 1},
    }
    plan_id = call_tool(ctx, "submit_edit_plan", {"brief_id": brief_id, "plan": plan})[
        "edit_plan_id"
    ]
    video = ctx.renders_dir / f"r{brief_id}.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"mp4")
    render_id = ctx.conn.execute(
        "INSERT INTO renders (edit_plan_id, path) VALUES (?, ?)", (plan_id, str(video))
    ).lastrowid
    ctx.conn.execute(
        "UPDATE posts SET state = ?, render_id = ? WHERE brief_id = ?", (state, render_id, brief_id)
    )
    if state == "qa_passed":  # the stored verdicts create_draft re-reads
        for check in ("rights", "disclosure", "banned_topics", "claims_about_abbas", "qa_review"):
            ctx.conn.execute(
                "INSERT INTO qa_results (render_id, check_name, result) VALUES (?, ?, 'pass')",
                (render_id, check),
            )
    return render_id


def test_create_draft_requires_qa_passed(ctx):
    render_id = rendered(ctx, "rendered")
    result = call_tool(ctx, "create_draft", {"render_id": render_id, "posts": [COPY]})
    assert result["ok"] is False and "run_qa_checks" in result["errors"][0]
    assert ctx.providers.publisher.drafts == {}


def test_create_draft_makes_a_private_youtube_draft_once(ctx):
    render_id = rendered(ctx, "qa_passed")
    result = call_tool(ctx, "create_draft", {"render_id": render_id, "posts": [COPY]})
    assert result["ok"]
    (draft,) = result["drafts"]
    payload = ctx.providers.publisher.drafts[draft["postiz_id"]]
    assert payload["type"] == "draft"  # GR-4: drafts only at L0
    settings = payload["posts"][0]["settings"]
    assert settings["type"] == "private" and settings["title"] == COPY["title"]  # ADR 0002
    content = payload["posts"][0]["value"][0]["content"]
    assert content.startswith(COPY["caption"]) and content.endswith("#discipline #habits")
    row = ctx.conn.execute("SELECT * FROM posts").fetchone()
    assert row["state"] == "drafted" and row["postiz_id"] == draft["postiz_id"]
    assert json.loads(row["hashtags"]) == COPY["hashtags"]
    assert row["scheduled_at"] == "2026-11-14T07:00:00Z"  # min_gap_hours after now
    again = call_tool(ctx, "create_draft", {"render_id": render_id, "posts": [COPY]})
    assert again["ok"] and len(ctx.providers.publisher.drafts) == 1


def test_create_draft_needs_copy_for_every_channel(ctx):
    render_id = rendered(ctx, "qa_passed")
    tiktok_only = {**COPY, "platform": "tiktok"}
    result = call_tool(ctx, "create_draft", {"render_id": render_id, "posts": [tiktok_only]})
    assert result["ok"] is False and "youtube" in result["errors"][0]


def test_create_draft_spaces_drafts_on_a_channel(ctx):
    for _ in range(2):
        render_id = rendered(ctx, "qa_passed")
        assert call_tool(ctx, "create_draft", {"render_id": render_id, "posts": [COPY]})["ok"]
    times = [r[0] for r in ctx.conn.execute("SELECT scheduled_at FROM posts ORDER BY id")]
    assert times == ["2026-11-14T07:00:00Z", "2026-11-14T10:00:00Z"]  # FR-PUB-4: 3 h apart


def test_create_draft_refuses_a_channel_gone_from_the_config(ctx):
    render_id = rendered(ctx, "qa_passed")
    selfhelp = ctx.config.identities["selfhelp"]
    tiktok = selfhelp.channels[0].model_copy(update={"platform": "tiktok"})
    ctx.config.identities["selfhelp"] = selfhelp.model_copy(update={"channels": [tiktok]})
    result = call_tool(ctx, "create_draft", {"render_id": render_id, "posts": [COPY]})
    assert result["ok"] is False and "no longer has a youtube channel" in result["errors"][0]


def test_render_post_refuses_a_voice_from_another_brief(ctx):
    _, script_a = scripted(ctx)
    voice_a = call_tool(ctx, "synthesize_voice", {"script_id": script_a})["asset_id"]
    brief_b, script_b = scripted(ctx)
    ctx.conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, duration, rights_type, "
        "people_release) VALUES ('engine', ?, 'b.mp4', 'video', 40, 'ai_generated', 'none')",
        ("e" * 64,),
    )
    video = ctx.conn.execute("SELECT id FROM assets WHERE path = 'b.mp4'").fetchone()[0]
    plan = {
        "timeline": [{"asset_id": video, "in": 0, "out": 30, "role": "avatar"}],
        "audio": {"voice": [{"asset_id": voice_a, "at": 0, "out": 20}]},
        "captions": {"block": "captions"},
        "look": {"id": "selfhelp", "version": 1},
    }
    plan_id = call_tool(ctx, "submit_edit_plan", {"brief_id": brief_b, "plan": plan})[
        "edit_plan_id"
    ]
    result = call_tool(ctx, "render_post", {"edit_plan_id": plan_id})
    assert result["ok"] is False and "not a voiced script of brief" in result["errors"][0]
