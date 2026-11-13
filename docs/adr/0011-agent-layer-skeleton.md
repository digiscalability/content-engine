# ADR 0011: Agent layer skeleton: tools, hooks and unattended SDK options

- Status: accepted, 2026-11-13 (slice P0.7a)
- Requirements: SRD §5.3–§5.6, §8.1–§8.3, §17, FR-AGT-1, FR-PLN-3, FR-PRD-1, GR-6, GR-7, GR-9, NFR-3

## Context

§5 fixes the agents, their tools and the guards, and names the Claude Agent SDK settings for unattended runs. How the tools and guards are built, which folders H2 opens, how FR-AGT-1 is met against the SDK's real behaviour, and how P0.7 is cut into evenings were open.

## Decision

1. **P0.7 is split** (build prompt: about three hours a slice): P0.7a this skeleton; P0.7b action tools and `ce daily --dry-run`; P0.7c QA checks, `render_report.json` and the FCPXML/OTIO exports (D12).
2. **Tools are plain Python first.** Each is a `ToolSpec` (name, description, pydantic input model, handler on a `ToolContext`); `call_tool()` validates and returns `{"ok": …}` or `{"ok": false, "errors": […]}`. `options.py` wraps them as the in-process MCP server `engine`, answering errors with `is_error: true`. Everything is testable without Claude.
3. **One rights check, three users.** `rights_violations()` serves `submit_edit_plan`, H4 and later FR-QA-1: tier per format, licence expiry on the asset and its source, no unreleased people (§8.3), and for film: evidence role only, never audio, each excerpt at most 10 s and all excerpts at most 30% of the runtime, both measured in **on-screen** time so speed or a freeze cannot stretch them (security review finding).
4. **Fail closed.** The four guards are registered with `on_failure="block"`, so a crashing guard blocks. Submit tools refuse work for a killed or unknown identity (GR-7; security review finding). H5 audit does not block: losing an audit row must not stop a run.
5. **Guards as built.** H1: L0, kill flag, not `qa_passed`, or any QA hold/fail; the §17 L1 entry criteria arrive with P5.1. H2: `Read` only under `config/`, `engine/prompts/` and `media/work/` (the SRD's "work/" in the §5.7 layout); the path is resolved to its real long name (junctions, symlinks, 8.3 short names) and must exist; alternate data streams, short-name segments and secret-looking names are refused (security review finding). H3: voice against the engine cap and account floor; renders against the identity's daily cap, counted by local date; HeyGen credits wait for P1.4. H4: `rights_violations()`.
6. **Tool access.** Every agent may use its §5.3 tools and `Read`; the orchestrator also has `Agent` for its subagents. Bash, web, write, edit and repo-wide search (`Glob`, `Grep`, which would bypass H2) are disallowed, and `permission_mode="dontAsk"` refuses anything not pre-approved.
7. **FR-AGT-1, against the SDK as it is.** The SDK starts the agent with this process's whole environment plus `options.env` (read in `subprocess_cli.py`, v0.2.181). So `options.env` holds only `ANTHROPIC_API_KEY`, and `scrub_process_env()` removes every other key name in `KEY_SPECS` from `os.environ` before a run.
8. **Config is the one home** for each agent's model, effort and turn limit (`engine.yaml`, C5); the code table holds only names, descriptions and tools. Prompts live in `engine/prompts/`; the orchestrator's goes as a file, subagents' travel in the SDK's initialize request (not the command line).

## Consequences

- P0.7b adds the action tools (`synthesize_voice`, `request_avatar`, `render_post`, `create_draft`) to the same registry; their names are already on the allowed lists and under H3.
- A live run needs `ANTHROPIC_API_KEY` (G0.3); until then nothing here calls Claude.
