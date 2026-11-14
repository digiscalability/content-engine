"""`ClaudeAgentOptions` for unattended runs (SRD §5.6) and the agent's environment (FR-AGT-1).

- `permission_mode="dontAsk"`: only pre-approved tools run; nothing waits for a human.
- `setting_sources=["project"]`: user-level plugins and hooks never load; the guards
  live in code (`hooks.py`).
- The agent subprocess inherits this process's environment plus `options.env`, so vendor
  keys are scrubbed from `os.environ` before a run and `options.env` carries only the
  Anthropic key (FR-AGT-1, ADR 0011).
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from claude_agent_sdk import ClaudeAgentOptions, SdkMcpTool, create_sdk_mcp_server, tool

from engine.agents.definitions import (
    AGENTS,
    BANNED_TOOLS,
    PROMPTS_DIR,
    agent_definitions,
    engine_tools,
)
from engine.agents.hooks import build_hooks
from engine.agents.schemas import RunReport
from engine.agents.tools import TOOLS, ToolContext, ToolSpec, call_tool
from engine.ops.budget import claude_run_budget
from engine.settings import KEY_SPECS, REPO_ROOT, Settings

AGENT_KEY = "ANTHROPIC_API_KEY"


def agent_env(settings: Settings) -> dict[str, str]:
    """The only variable the engine adds to the agent's environment."""
    key = settings.get(AGENT_KEY)
    return {AGENT_KEY: key} if key else {}


def scrub_process_env() -> list[str]:
    """Remove vendor keys from this process's environment before an agent starts."""
    removed = [s.name for s in KEY_SPECS if s.name != AGENT_KEY and s.name in os.environ]
    for name in removed:
        del os.environ[name]
    return removed


def sdk_tool(ctx: ToolContext, spec: ToolSpec) -> SdkMcpTool[Any]:
    async def run(args: dict) -> dict:
        result = call_tool(ctx, spec.name, args)
        return {
            "content": [{"type": "text", "text": json.dumps(result, default=str)}],
            "is_error": not result["ok"],
        }

    return tool(spec.name, spec.description, spec.input_schema)(run)


def sdk_tools(ctx: ToolContext) -> list[SdkMcpTool[Any]]:
    return [sdk_tool(ctx, spec) for spec in TOOLS.values()]


def engine_server(ctx: ToolContext):
    """The in-process MCP server; its tools are `mcp__engine__<name>`."""
    return create_sdk_mcp_server(name="engine", tools=sdk_tools(ctx))


def build_options(
    ctx: ToolContext, settings: Settings, command: Literal["daily", "weekly"] = "daily"
) -> ClaudeAgentOptions:
    """The orchestrator's options; `BudgetStop` when the month's Claude cap is spent."""
    engine = ctx.config.engine
    orchestrator = engine.agents.orchestrator
    return ClaudeAgentOptions(
        model=orchestrator.model,
        effort=orchestrator.effort,
        max_turns=orchestrator.max_turns,
        max_budget_usd=claude_run_budget(ctx.conn, ctx.config, ctx.month, command),
        permission_mode="dontAsk",
        allowed_tools=[*engine_tools(AGENTS["orchestrator"].tools), "Read", "Agent"],
        disallowed_tools=list(BANNED_TOOLS),
        mcp_servers={"engine": engine_server(ctx)},
        hooks=build_hooks(ctx),
        agents=agent_definitions(ctx.config),
        cwd=str(REPO_ROOT),
        setting_sources=["project"],
        env=agent_env(settings),
        system_prompt={"type": "file", "path": str(PROMPTS_DIR / "orchestrator.md")},
        output_format={"type": "json_schema", "schema": RunReport.model_json_schema()},
    )
