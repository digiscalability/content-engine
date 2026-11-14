"""Hooks H1-H5 (SRD §5.5): deny tests for every reason, allow tests, and the SDK wiring."""

import asyncio
import json
from datetime import date

import pytest
from claude_agent_sdk import HookMatcher

from engine.agents import hooks
from engine.agents.state import start_run, sync_identities
from engine.agents.tools import ToolContext, call_tool
from engine.config import load_config
from engine.db import open_db
from engine.produce.voice import Balance
from engine.settings import REPO_ROOT

TODAY = date.today()  # H3 counts renders by local date
BRIEF = {
    "identity_id": "selfhelp",
    "format": "C",
    "topic": "focus",
    "angle": "craft",
    "hook": "One habit",
    "cta": "follow",
    "difference_note": "New list structure",
}


@pytest.fixture
def ctx(tmp_path, repo_root):
    config = load_config(repo_root / "config")
    conn = open_db(tmp_path / "engine.db")
    sync_identities(conn, config)
    context = ToolContext(
        conn=conn, config=config, config_dir=repo_root / "config", today=TODAY, run_id="run-1"
    )
    start_run(conn, "run-1", "ce daily --dry-run")
    return context


def own_asset(ctx, people="none") -> int:
    ctx.conn.execute(
        "INSERT OR IGNORE INTO sources (id, kind, path, rights_type, rights_proof) "
        "VALUES ('own', 'folder', 'C:/x', 'own', 'Filmed by Abbas')"
    )
    n = ctx.conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    return ctx.conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, rights_type, people_release) "
        "VALUES ('own', ?, 'C:/a.mp4', 'video', 'own', ?)",
        (f"{n:064d}", people),
    ).lastrowid


def plan_input(brief_id: int, asset_id: int) -> dict:
    return {
        "brief_id": brief_id,
        "plan": {
            "timeline": [{"asset_id": asset_id, "in": 0, "out": 30, "role": "avatar"}],
            "look": {"id": "selfhelp", "version": 1},
        },
    }


def post(ctx, state="qa_passed", flag=None) -> int:
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    asset_id = own_asset(ctx)
    plan_id = call_tool(ctx, "submit_edit_plan", plan_input(brief_id, asset_id))["edit_plan_id"]
    render_id = ctx.conn.execute(
        "INSERT INTO renders (edit_plan_id, path) VALUES (?, 'r.mp4')", (plan_id,)
    ).lastrowid
    if flag:
        ctx.conn.execute(
            "INSERT INTO qa_results (render_id, check_name, result) VALUES (?, 'qa_review', ?)",
            (render_id, flag),
        )
    ctx.conn.execute(  # submit_brief planned the row; put it where the test needs it
        "UPDATE posts SET render_id = ?, state = ? WHERE brief_id = ?",
        (render_id, state, brief_id),
    )
    return ctx.conn.execute("SELECT id FROM posts WHERE brief_id = ?", (brief_id,)).fetchone()[0]


def at_level_1(ctx):
    identity = ctx.config.identities["selfhelp"]
    ctx.config.identities["selfhelp"] = identity.model_copy(update={"autonomy_level": 1})


# H1 publish guard -------------------------------------------------------------------------


def test_h1_denies_scheduling_at_l0(ctx):
    assert "L0" in hooks.h1_publish(ctx, {"post_id": post(ctx)})


def test_h1_denies_a_killed_identity(ctx):
    at_level_1(ctx)
    post_id = post(ctx)
    ctx.conn.execute("UPDATE identities SET kill_flag = 1")
    assert "kill" in hooks.h1_publish(ctx, {"post_id": post_id})


def test_h1_denies_a_post_that_has_not_passed_qa(ctx):
    at_level_1(ctx)
    assert "qa_passed" in hooks.h1_publish(ctx, {"post_id": post(ctx, state="rendered")})


def test_h1_denies_a_post_with_a_qa_flag(ctx):
    at_level_1(ctx)
    assert "QA flag" in hooks.h1_publish(ctx, {"post_id": post(ctx, flag="hold")})


def test_h1_denies_an_unknown_post(ctx):
    assert "no post" in hooks.h1_publish(ctx, {"post_id": 99})


def test_h1_allows_a_clean_post_at_l1(ctx):
    at_level_1(ctx)
    assert hooks.h1_publish(ctx, {"post_id": post(ctx)}) is None


# H2 path guard ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "config/engine.yaml",
        "engine/prompts/scriptwriter.md",
        str(REPO_ROOT / "config" / "identities" / "selfhelp.playbook.md"),
    ],
)
def test_h2_allows_reads_inside_the_allowed_folders(path):
    assert hooks.h2_read_path({"file_path": path}) is None


@pytest.mark.parametrize(
    "path",
    ["README.md", "engine/settings.py", "C:/Windows/win.ini", "pyproject.toml"],
)
def test_h2_denies_reads_outside_the_allowed_folders(path):
    assert "outside" in hooks.h2_read_path({"file_path": path})


@pytest.mark.parametrize(
    "path",
    [
        "config/../.env",
        "CONFIG/.ENV.local",
        "config/secrets.yaml",
        "config/credentials.json",
        "media/work/key.pem",
        "config/serviceAccount-prod.json",
        "media\\work\\..\\..\\.env",
    ],
)
def test_h2_denies_traversal_and_case_tricks(path):
    assert hooks.h2_read_path({"file_path": path}) is not None


def test_h2_denies_a_missing_path():
    assert hooks.h2_read_path({}) is not None


# H3 budget guard --------------------------------------------------------------------------


def script(ctx, chars: int) -> int:
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    beats = [{"text": "x" * (chars // 8), "target_s": 5}] * 8
    return call_tool(ctx, "submit_script", {"brief_id": brief_id, "beats": beats})["script_id"]


def test_h3_denies_voice_past_the_engine_cap(ctx):
    ctx.conn.execute(
        "INSERT INTO budget_ledger (vendor, month, units, charge_key) "
        "VALUES ('elevenlabs', '2026-11', 78500, 'earlier')"
    )
    reason = hooks.h3_budget(ctx, "mcp__engine__synthesize_voice", {"script_id": script(ctx, 800)})
    assert "engine cap" in reason


def test_h3_denies_voice_below_the_account_floor(ctx):
    ctx.voice_balance = Balance(used=110_500, limit=131_000)
    reason = hooks.h3_budget(ctx, "mcp__engine__synthesize_voice", {"script_id": script(ctx, 800)})
    assert "account floor" in reason


def test_h3_allows_voice_inside_the_limits(ctx):
    ctx.voice_balance = Balance(used=10_000, limit=131_000)
    assert (
        hooks.h3_budget(ctx, "mcp__engine__synthesize_voice", {"script_id": script(ctx, 800)})
        is None
    )


def test_h3_denies_renders_past_the_daily_cap(ctx):
    post(ctx)  # one render today; selfhelp allows 1 a day
    plan_id = ctx.conn.execute("SELECT MAX(id) FROM edit_plans").fetchone()[0]
    assert "a day" in hooks.h3_budget(ctx, "mcp__engine__render_post", {"edit_plan_id": plan_id})


def test_h3_allows_the_first_render_of_the_day(ctx):
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    plan_id = call_tool(ctx, "submit_edit_plan", plan_input(brief_id, own_asset(ctx)))[
        "edit_plan_id"
    ]
    assert hooks.h3_budget(ctx, "mcp__engine__render_post", {"edit_plan_id": plan_id}) is None


def voiced(ctx, seconds: float) -> int:
    """A script with its voice WAV where synthesize_voice leaves it."""
    from engine.produce.voice import silent_wav_bytes

    script_id = script(ctx, 800)
    wav = ctx.work_dir / "voice" / f"script-{script_id}.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    wav.write_bytes(silent_wav_bytes(seconds))
    return script_id


def test_h3_allows_an_avatar_inside_the_heygen_cap(ctx):
    script_id = voiced(ctx, 45)
    args = {"brief_id": 1, "script_id": script_id}
    assert hooks.h3_budget(ctx, "mcp__engine__request_avatar", args) is None


def test_h3_denies_an_avatar_past_the_heygen_cap(ctx):
    cap = ctx.config.engine.budgets.heygen.monthly_credit_cap
    ctx.conn.execute(
        "INSERT INTO budget_ledger (vendor, month, units, charge_key) VALUES ('heygen', ?, ?, 'x')",
        (ctx.month, cap - 0.1),
    )
    args = {"brief_id": 1, "script_id": voiced(ctx, 45)}
    assert "HeyGen" in hooks.h3_budget(ctx, "mcp__engine__request_avatar", args)


def test_h3_reads_the_avatar_input_as_the_tool_does(ctx):
    cap = ctx.config.engine.budgets.heygen.monthly_credit_cap
    ctx.conn.execute(
        "INSERT INTO budget_ledger (vendor, month, units, charge_key) VALUES ('heygen', ?, ?, 'x')",
        (ctx.month, cap - 0.1),
    )
    script_id = voiced(ctx, 45)
    as_float = {"brief_id": 1, "script_id": float(script_id)}  # the tool coerces 2.0 to 2
    assert "HeyGen" in hooks.h3_budget(ctx, "mcp__engine__request_avatar", as_float)


def test_h3_denies_an_avatar_request_it_cannot_read(ctx):
    for bad in (
        {"brief_id": 1, "script_id": "two"},
        {"brief_id": 1},
        {"brief_id": 1, "script_id": 99},
    ):
        assert hooks.h3_budget(ctx, "mcp__engine__request_avatar", bad) is not None


def test_h3_denies_a_render_when_the_disk_is_nearly_full(ctx):
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    plan_id = call_tool(ctx, "submit_edit_plan", plan_input(brief_id, own_asset(ctx)))[
        "edit_plan_id"
    ]
    ctx.disk_free = lambda path: 1024**3  # 1 GB
    assert "NFR-5" in hooks.h3_budget(ctx, "mcp__engine__render_post", {"edit_plan_id": plan_id})


# H4 rights guard --------------------------------------------------------------------------


def test_h4_denies_a_plan_with_unreleased_people(ctx):
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    reason = hooks.h4_rights(ctx, plan_input(brief_id, own_asset(ctx, people="unknown")))
    assert "§8.3" in reason


def test_h4_allows_a_clean_plan(ctx):
    brief_id = call_tool(ctx, "submit_brief", BRIEF)["brief_id"]
    assert hooks.h4_rights(ctx, plan_input(brief_id, own_asset(ctx))) is None


# H5 audit ---------------------------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


def test_h5_records_every_tool_call(ctx):
    audit = hooks.Audit(ctx)
    pre = {"hook_event_name": "PreToolUse", "tool_name": "mcp__engine__get_state", "tool_input": {}}
    post_ = {**pre, "hook_event_name": "PostToolUse", "tool_response": {"content": []}}
    assert run(audit.before(pre, "tu-1", None)) == {}
    assert run(audit.after(post_, "tu-1", None)) == {}
    fail = {**pre, "hook_event_name": "PostToolUseFailure", "error": "boom"}
    run(audit.before(pre, "tu-2", None))
    run(audit.failed(fail, "tu-2", None))
    rows = ctx.conn.execute("SELECT * FROM tool_calls ORDER BY id").fetchall()
    assert [(r["run_id"], r["tool"], r["status"]) for r in rows] == [
        ("run-1", "mcp__engine__get_state", "ok"),
        ("run-1", "mcp__engine__get_state", "error"),
    ]
    assert len(rows[0]["input_digest"]) == 64 and rows[0]["duration_ms"] >= 0


# SDK wiring -------------------------------------------------------------------------------


def test_deny_callback_speaks_the_sdk_hook_format(ctx):
    callback = hooks.guard(lambda tool_name, tool_input: "no, because")
    out = run(
        callback(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "x"},
            },
            "tu",
            None,
        )
    )
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "no, because",
        }
    }
    allow = hooks.guard(lambda tool_name, tool_input: None)
    assert (
        run(
            allow(
                {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {}}, "tu", None
            )
        )
        == {}
    )


def test_hooks_register_with_on_failure_block(ctx):
    registered = hooks.build_hooks(ctx)
    guards = registered["PreToolUse"][:4]
    assert all(isinstance(m, HookMatcher) and m.on_failure == "block" for m in guards)
    assert [m.matcher for m in guards] == [
        "mcp__engine__schedule_post",
        "Read",
        "mcp__engine__synthesize_voice|mcp__engine__request_avatar|mcp__engine__render_post",
        "mcp__engine__submit_edit_plan",
    ]
    assert registered["PostToolUse"][0].matcher is None
    assert "PostToolUseFailure" in registered
    json.dumps([m.matcher for m in registered["PreToolUse"]])


def test_h2_allows_an_existing_file_in_a_work_folder(tmp_path):
    work = tmp_path / "work"
    (work / "brief-1").mkdir(parents=True)
    (work / "brief-1" / "mix.wav").write_bytes(b"RIFF")
    assert (
        hooks.h2_read_path({"file_path": str(work / "brief-1" / "mix.wav")}, roots=(work,)) is None
    )


@pytest.mark.parametrize(
    "path",
    ["config/engine.yaml:hidden", "config/engine.yaml::$DATA", "config/SECRET~1.YAM"],
)
def test_h2_denies_streams_and_short_names(path):
    assert hooks.h2_read_path({"file_path": path}) is not None


def test_h2_denies_a_path_that_does_not_exist():
    assert "exist" in hooks.h2_read_path({"file_path": "config/no-such-file.yaml"})


def test_h2_denies_a_junction_out_of_an_allowed_folder(tmp_path):
    _winapi = pytest.importorskip("_winapi")
    work, outside = tmp_path / "work", tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    (outside / "notes.txt").write_text("private", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(work / "link"))
    reason = hooks.h2_read_path({"file_path": str(work / "link" / "notes.txt")}, roots=(work,))
    assert "outside" in reason


# One guard table for the SDK hooks and the dry run ---------------------------------------


def tool_rows(ctx) -> list[tuple[str, str]]:
    rows = ctx.conn.execute("SELECT tool, status FROM tool_calls ORDER BY id")
    return [(r["tool"], r["status"]) for r in rows]


def test_the_sdk_hooks_are_built_from_the_guard_table(ctx):
    table = hooks.guard_table(ctx)
    registered = hooks.build_hooks(ctx)["PreToolUse"][: len(table)]
    assert [g.matcher for g in table] == [m.matcher for m in registered]
    assert [g.name for g in table] == ["H1", "H2", "H3", "H4"]


def test_guarded_tools_deny_as_the_hooks_do_and_audit_the_call(ctx):
    tools = hooks.GuardedTools(ctx)
    brief_id = tools.call("submit_brief", BRIEF)["brief_id"]
    denied = tools.call("submit_edit_plan", plan_input(brief_id, own_asset(ctx, people="unknown")))
    assert denied["ok"] is False and denied["denied"] is True
    assert "(H4)" in denied["errors"][0]
    assert ctx.conn.execute("SELECT COUNT(*) FROM edit_plans").fetchone()[0] == 0
    assert tool_rows(ctx) == [
        ("mcp__engine__submit_brief", "ok"),
        ("mcp__engine__submit_edit_plan", "denied"),
    ]


def test_guarded_tools_record_a_refused_input_as_an_error(ctx):
    tools = hooks.GuardedTools(ctx)
    out = tools.call("submit_script", {"brief_id": 999, "beats": [{"text": "x", "target_s": 30}]})
    assert out["ok"] is False and "denied" not in out
    assert tool_rows(ctx) == [("mcp__engine__submit_script", "error")]


def test_a_guard_that_crashes_blocks_the_call(ctx, monkeypatch):
    def broken(ctx_, args):
        raise RuntimeError("db gone")

    monkeypatch.setattr(hooks, "h4_rights", broken)
    tools = hooks.GuardedTools(ctx)
    brief_id = tools.call("submit_brief", BRIEF)["brief_id"]
    out = tools.call("submit_edit_plan", plan_input(brief_id, own_asset(ctx)))
    assert out["denied"] is True and "H4 failed" in out["errors"][0]
    assert ctx.conn.execute("SELECT COUNT(*) FROM edit_plans").fetchone()[0] == 0


# H2 before any file system call (automated security review of the public push) -------------


@pytest.mark.parametrize(
    "path",
    [
        r"\\attacker.example\share\config\engine.yaml",  # UNC: would send NTLM
        "//attacker.example/share/engine.yaml",
        r"\\?\C:\Windows\win.ini",  # device namespace
        r"\\.\pipe\x",
        "Z:/config/engine.yaml",  # another drive, maybe a mapped share
    ],
)
def test_h2_refuses_network_device_and_other_drive_paths_without_touching_them(path, monkeypatch):
    def no_lookup(*args, **kwargs):
        raise AssertionError("H2 resolved a path it should have refused first")

    monkeypatch.setattr(hooks.os.path, "realpath", no_lookup)
    reason = hooks.h2_read_path({"file_path": path})
    assert reason is not None and "(H2)" in reason


@pytest.mark.parametrize("name", ["deploy.key", "cert.p12", "cert.pfx"])
def test_h2_refuses_the_same_secret_file_types_as_the_commit_scan(name, tmp_path):
    secret = tmp_path / name
    secret.write_text("x", encoding="utf-8")
    reason = hooks.h2_read_path({"file_path": str(secret)}, roots=(tmp_path,))
    assert reason is not None and "secret" in reason
