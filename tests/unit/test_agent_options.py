"""Agent definitions and SDK options for unattended runs (SRD §5.3, §5.6, FR-AGT-1, GR-9)."""

import asyncio
import json
import os

import pytest
from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions
from pydantic import SecretStr

from engine.agents.definitions import AGENTS, BANNED_TOOLS, agent_definitions
from engine.agents.options import agent_env, build_options, engine_server, scrub_process_env
from engine.agents.schemas import RunReport
from engine.agents.state import sync_identities
from engine.agents.tools import ToolContext
from engine.config import load_config
from engine.db import open_db
from engine.settings import KEY_SPECS, REPO_ROOT, Settings

FAKE = {spec.name: f"value-of-{spec.name.lower()}-" + "z" * 12 for spec in KEY_SPECS}
VENDOR_NAMES = {name for name in FAKE if name != "ANTHROPIC_API_KEY"}


@pytest.fixture
def ctx(tmp_path, repo_root):
    config = load_config(repo_root / "config")
    conn = open_db(tmp_path / "engine.db")
    sync_identities(conn, config)
    return ToolContext(conn=conn, config=config, config_dir=repo_root / "config")


@pytest.fixture
def settings():
    return Settings({name: SecretStr(value) for name, value in FAKE.items()})


def engine(names):
    return [f"mcp__engine__{n}" for n in names]


def test_section_5_3_tools_per_agent():
    assert AGENTS["orchestrator"].tools == (
        "get_state",
        "get_playbook",
        "get_topic_candidates",
        "get_style_profile",
        "get_trends",
        "budget_status",
        "synthesize_voice",
        "request_avatar",
        "render_post",
        "run_qa_checks",
        "create_draft",
    )
    assert AGENTS["scriptwriter"].tools == (
        "get_playbook",
        "get_topic_candidates",
        "get_style_profile",
        "search_library",
        "submit_brief",
        "submit_script",
        "submit_edit_plan",
    )
    assert AGENTS["moment-scorer"].tools == ("search_library", "submit_moment_scores")
    assert AGENTS["qa-reviewer"].tools == ("get_playbook", "get_render_report", "submit_qa_review")


def test_effort_and_turns_follow_engine_yaml(ctx):
    agents = ctx.config.engine.agents
    expected = {
        "orchestrator": ("medium", 60),
        "scriptwriter": ("high", 25),
        "moment_scorer": ("low", 15),
        "qa_reviewer": ("high", 10),
    }
    for key, (effort, turns) in expected.items():
        assert (getattr(agents, key).effort, getattr(agents, key).max_turns) == (effort, turns)


def test_subagents_are_sdk_definitions_with_prompts_from_files(ctx):
    definitions = agent_definitions(ctx.config)
    assert set(definitions) == {"scriptwriter", "moment-scorer", "qa-reviewer"}
    writer = definitions["scriptwriter"]
    assert isinstance(writer, AgentDefinition)
    assert writer.prompt == (REPO_ROOT / "engine" / "prompts" / "scriptwriter.md").read_text(
        encoding="utf-8"
    )
    assert writer.tools == [*engine(AGENTS["scriptwriter"].tools), "Read"]
    assert writer.model == "claude-opus-5-5" and writer.maxTurns == 25 and writer.effort == "high"
    assert set(writer.disallowedTools) == set(BANNED_TOOLS)


def test_no_agent_can_run_commands_browse_or_write():
    assert {"Bash", "WebSearch", "WebFetch", "Write", "Edit", "Glob", "Grep"} <= set(BANNED_TOOLS)
    for agent in AGENTS.values():
        assert not set(agent.tools) & set(BANNED_TOOLS)


def test_options_for_an_unattended_run(ctx, settings):
    options = build_options(ctx, settings)
    assert isinstance(options, ClaudeAgentOptions)
    assert options.permission_mode == "dontAsk"
    assert options.can_use_tool is None
    assert options.setting_sources == ["project"]
    assert options.cwd == str(REPO_ROOT)
    assert (options.model, options.effort, options.max_turns) == ("claude-opus-5-5", "medium", 60)
    assert options.max_budget_usd == 3
    assert options.allowed_tools == [*engine(AGENTS["orchestrator"].tools), "Read", "Agent"]
    assert set(BANNED_TOOLS) <= set(options.disallowed_tools)
    assert set(options.mcp_servers) == {"engine"}
    assert set(options.hooks) == {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
    assert options.system_prompt == {
        "type": "file",
        "path": str(REPO_ROOT / "engine/prompts/orchestrator.md"),
    }
    assert options.output_format == {"type": "json_schema", "schema": RunReport.model_json_schema()}


def test_fr_agt_1_agent_env_carries_no_vendor_key(ctx, settings):
    """FR-AGT-1 accept: the env passed to the SDK contains no vendor key names."""
    env = build_options(ctx, settings).env
    assert set(env) == {"ANTHROPIC_API_KEY"}
    assert not VENDOR_NAMES & set(env)
    assert not any(FAKE[name] in json.dumps(env) for name in VENDOR_NAMES)


def test_agent_env_is_empty_without_an_anthropic_key():
    assert agent_env(Settings({})) == {}


def test_scrub_removes_vendor_keys_from_process_env(monkeypatch, ctx, settings):
    monkeypatch.setenv("POSTIZ_API_KEY", FAKE["POSTIZ_API_KEY"])
    monkeypatch.setenv("ELEVENLABS_API_KEY", FAKE["ELEVENLABS_API_KEY"])
    removed = scrub_process_env()
    assert set(removed) == {"POSTIZ_API_KEY", "ELEVENLABS_API_KEY"}
    # what the SDK gives the agent subprocess: {**os.environ, **options.env}
    agent_process_env = {**os.environ, **build_options(ctx, settings).env}
    assert not VENDOR_NAMES & set(agent_process_env)


def test_engine_tools_answer_through_the_sdk_wrapper(ctx):
    from engine.agents.options import sdk_tools

    server = engine_server(ctx)
    assert (server["type"], server["name"]) == ("sdk", "engine")
    tools = {t.name: t for t in sdk_tools(ctx)}
    ok = asyncio.run(tools["get_state"].handler({}))
    assert ok.get("is_error") is False
    assert json.loads(ok["content"][0]["text"])["identities"][0]["id"] == "selfhelp"
    bad = asyncio.run(tools["get_playbook"].handler({"identity": "nobody"}))
    assert bad["is_error"] is True


def test_the_run_budget_shrinks_to_what_is_left_of_the_month(ctx, settings):
    from datetime import date

    from engine.ops.budget import BudgetStop

    ctx.today = date(2026, 11, 14)
    ctx.conn.execute(
        "INSERT INTO budget_ledger (vendor, month, units, cost, charge_key) "
        "VALUES ('anthropic', '2026-11', 58.5, 58.5, 'earlier')"
    )
    assert build_options(ctx, settings).max_budget_usd == 1.5
    ctx.conn.execute("UPDATE budget_ledger SET cost = 60")
    with pytest.raises(BudgetStop, match="monthly cap"):
        build_options(ctx, settings)
