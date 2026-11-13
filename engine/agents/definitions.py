"""The agents of SRD §5.3: tools per agent, prompts from files; model, effort and turn
limits come from `engine.yaml` (their only home).

Only Phase 0 agents are defined; trend-scout (Phase 2), retro-analyst (Phase 3) and
source-scout (Phase 4) join with their slices. Model ids come from `engine.yaml`, so
moving a worker to a cheaper model is a config change (C5).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import AgentDefinition

from engine.config import Config
from engine.settings import REPO_ROOT

PROMPTS_DIR = REPO_ROOT / "engine" / "prompts"
# GR-9 and §5.4: no shell, no web, no file writes, no repo-wide search (Read alone is
# hook-guarded to config/, engine/prompts/ and media/work/)
BANNED_TOOLS = (
    "Bash",
    "BashOutput",
    "KillShell",
    "Monitor",
    "WebSearch",
    "WebFetch",
    "Write",
    "Edit",
    "NotebookEdit",
    "Glob",
    "Grep",
)


@dataclass(frozen=True)
class AgentSpec:
    name: str
    config_key: str  # its entry under `agents:` in engine.yaml
    description: str
    tools: tuple[str, ...]  # engine tool names, without the mcp__engine__ prefix

    @property
    def prompt_file(self) -> Path:
        return PROMPTS_DIR / f"{self.name}.md"


AGENTS: dict[str, AgentSpec] = {
    spec.name: spec
    for spec in (
        AgentSpec(
            "orchestrator",
            "orchestrator",
            "Runs the daily loop for each due identity and returns the run report.",
            (
                "get_state",
                "get_playbook",
                "get_style_profile",
                "get_trends",
                "budget_status",
                "synthesize_voice",
                "request_avatar",
                "render_post",
                "run_qa_checks",
                "create_draft",
            ),
        ),
        AgentSpec(
            "scriptwriter",
            "scriptwriter",
            "Writes and stores the brief, script and edit plan for one post.",
            (
                "get_playbook",
                "get_style_profile",
                "search_library",
                "submit_brief",
                "submit_script",
                "submit_edit_plan",
            ),
        ),
        AgentSpec(
            "moment-scorer",
            "moment_scorer",
            "Scores candidate moments for hook strength and standalone value.",
            ("search_library", "submit_moment_scores"),
        ),
        AgentSpec(
            "qa-reviewer",
            "qa_reviewer",
            "Judges a render against the identity's rules after the automatic checks pass.",
            ("get_playbook", "get_render_report", "submit_qa_review"),
        ),
    )
}
SUBAGENTS = ("scriptwriter", "moment-scorer", "qa-reviewer")


def engine_tools(names: tuple[str, ...]) -> list[str]:
    return [f"mcp__engine__{name}" for name in names]


def agent_definitions(config: Config) -> dict[str, AgentDefinition]:
    """The orchestrator's subagents as SDK definitions (sent in the initialize request)."""
    definitions = {}
    for name in SUBAGENTS:
        spec = AGENTS[name]
        model = getattr(config.engine.agents, spec.config_key)
        definitions[name] = AgentDefinition(
            description=spec.description,
            prompt=spec.prompt_file.read_text(encoding="utf-8"),
            tools=[*engine_tools(spec.tools), "Read"],
            disallowedTools=list(BANNED_TOOLS),
            model=model.model,
            maxTurns=model.max_turns,
            effort=model.effort,
        )
    return definitions
