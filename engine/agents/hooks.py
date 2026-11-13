"""Hooks H1-H5 (SRD §5.5): the guards around every agent tool call.

Each guard is a pure decision function returning a reason to deny, or None. `guard()`
turns one into an Agent SDK `PreToolUse` callback, and `build_hooks()` registers them with
`on_failure="block"`, so a guard that crashes blocks the call instead of waving it
through. Matchers see only tool names; the callbacks read `tool_input`.

H1 publish guard   schedule_post: L0, kill flag, not qa_passed, or any QA flag -> deny
H2 path guard      Read: outside config/, engine/prompts/, media/work/, or a secret name
H3 budget guard    voice: engine cap (D10) / account floor (C16); render: daily cap (GR-6)
H4 rights guard    submit_edit_plan: tiers, people, licences, film limits (§8.1-§8.3)
H5 audit           every call -> one `tool_calls` row (never denies)

`guard_table()` is the one list of guards. `build_hooks()` registers it with the SDK;
`GuardedTools` applies it to the dry run's scripted calls, so both paths are guarded alike.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher
from pydantic import ValidationError

from engine.agents.schemas import EditPlanSubmission, ScriptSubmission
from engine.agents.tools import ToolContext, call_tool, rights_violations
from engine.produce.voice import engine_characters
from engine.settings import REPO_ROOT

ENGINE = "mcp__engine__"
H3_MATCHER = "|".join(f"{ENGINE}{t}" for t in ("synthesize_voice", "request_avatar", "render_post"))
READ_ROOTS = tuple(REPO_ROOT / p for p in ("config", "engine/prompts", "media/work"))
# the commit scan's secret-bearing types too (scripts/check_secrets.py)
SECRET_NAME = re.compile(
    r"^\.env|secret|credential|\.pem$|\.key$|\.p12$|\.pfx$|serviceaccount", re.IGNORECASE
)
SHORT_NAME = re.compile(r"~\d")  # an 8.3 short-name segment such as CREDEN~1.JSO

Decision = Callable[[str, dict], "str | None"]


def _real(path: Path, strict: bool) -> str:
    """The real, long-name, case-folded path: follows junctions and symlinks and expands
    Windows 8.3 short names, so comparisons see what the file system will open."""
    return os.path.normcase(os.path.realpath(path, strict=strict))


# H1 ----------------------------------------------------------------------------------------


def h1_publish(ctx: ToolContext, tool_input: dict) -> str | None:
    row = ctx.conn.execute(
        "SELECT p.state, p.render_id, b.identity_id, i.kill_flag FROM posts p "
        "JOIN briefs b ON b.id = p.brief_id JOIN identities i ON i.id = b.identity_id "
        "WHERE p.id = ?",
        (tool_input.get("post_id"),),
    ).fetchone()
    if row is None:
        return f"no post {tool_input.get('post_id')}"
    identity = ctx.config.identities.get(row["identity_id"])
    if identity is None or identity.autonomy_level < 1:
        return f"{row['identity_id']} is at L0: drafts only, Abbas schedules (§17)"
    if row["kill_flag"]:
        return f"{row['identity_id']} has its kill flag set (GR-7)"
    if row["state"] != "qa_passed":
        return f"post is {row['state']}; only qa_passed posts are scheduled"
    flagged = ctx.conn.execute(
        "SELECT check_name FROM qa_results WHERE render_id = ? AND result IN ('hold', 'fail')",
        (row["render_id"],),
    ).fetchone()
    if flagged:
        return f"QA flag present ({flagged['check_name']}); holds always wait for Abbas"
    return None


# H2 ----------------------------------------------------------------------------------------


def h2_read_path(tool_input: dict, roots: tuple[Path, ...] = READ_ROOTS) -> str | None:
    raw = tool_input.get("file_path") or tool_input.get("path")
    if not raw:
        return "Read needs a file_path (H2)"
    text = str(raw).replace("\\", "/")
    # Refused before any file system call: resolving a UNC path would send this machine's
    # credentials to that host, and another drive may be a mapped network share.
    if text.startswith("//"):
        return f"Read of {raw} denied: network and device paths are not read (H2)"
    drive, rest = os.path.splitdrive(text)
    if drive and os.path.normcase(drive) != os.path.normcase(REPO_ROOT.drive):
        return f"Read of {raw} denied: only the repo's own drive is read (H2)"
    if ":" in rest:
        return f"Read of {raw} denied: alternate data streams are not read (H2)"
    if SHORT_NAME.search(rest):
        return f"Read of {raw} denied: use the long file name, not an 8.3 short name (H2)"
    path = Path(text)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if any(SECRET_NAME.search(part) for part in path.parts):
        return f"Read of {raw} denied: secret-looking name (H2)"
    outside = f"Read of {raw} denied: outside config/, engine/prompts/ and media/work/ (H2)"
    lexical = os.path.normcase(os.path.normpath(path))
    lexical_roots = [os.path.normcase(os.path.normpath(root)) for root in roots]
    if not any(lexical == r or lexical.startswith(r + os.sep) for r in lexical_roots):
        return outside  # only paths inside the roots are ever resolved
    try:
        resolved = _real(path, strict=True)
    except OSError:
        return f"Read of {raw} denied: it does not exist (H2)"
    if any(SECRET_NAME.search(part) for part in Path(resolved).parts):
        return f"Read of {raw} denied: secret-looking name (H2)"
    for root in roots:
        real_root = _real(root, strict=False)
        if resolved == real_root or resolved.startswith(real_root + os.sep):
            return None
    return outside


# H3 ----------------------------------------------------------------------------------------


def h3_budget(ctx: ToolContext, tool_name: str, tool_input: dict) -> str | None:
    if tool_name == f"{ENGINE}synthesize_voice":
        row = ctx.conn.execute(
            "SELECT body FROM scripts WHERE id = ?", (tool_input.get("script_id"),)
        ).fetchone()
        if row is None:
            return f"no script {tool_input.get('script_id')}"
        chars = len(ScriptSubmission.model_validate_json(row["body"]).spoken_text)
        budget = ctx.config.engine.budgets.elevenlabs
        used = engine_characters(ctx.conn, ctx.month)
        if used + chars > budget.monthly_char_cap:
            return f"engine cap: {used} + {chars} characters > {budget.monthly_char_cap} (D10)"
        if ctx.voice_balance and ctx.voice_balance.remaining - chars < budget.account_floor_chars:
            return (
                f"account floor: {ctx.voice_balance.remaining} left - {chars} "
                f"< {budget.account_floor_chars} (C16)"
            )
        return None
    if tool_name == f"{ENGINE}render_post":
        row = ctx.conn.execute(
            "SELECT b.identity_id FROM edit_plans e JOIN briefs b ON b.id = e.brief_id "
            "WHERE e.id = ?",
            (tool_input.get("edit_plan_id"),),
        ).fetchone()
        if row is None:
            return f"no edit plan {tool_input.get('edit_plan_id')}"
        identity = ctx.config.identities[row["identity_id"]]
        today = ctx.conn.execute(
            "SELECT COUNT(*) FROM renders r JOIN edit_plans e ON e.id = r.edit_plan_id "
            "JOIN briefs b ON b.id = e.brief_id "
            "WHERE b.identity_id = ? AND date(r.created_at, 'localtime') = ?",
            (identity.id, ctx.today.isoformat()),
        ).fetchone()[0]
        cap = identity.cadence.max_per_channel_per_day
        if today >= cap:
            return f"{identity.id} already has {today} render(s) today; cap is {cap} a day (GR-6)"
        return None
    return None  # request_avatar: HeyGen credit caps arrive with the budget ledger (P1.4)


# H4 ----------------------------------------------------------------------------------------


def h4_rights(ctx: ToolContext, tool_input: dict) -> str | None:
    try:
        submission = EditPlanSubmission.model_validate(tool_input)
    except ValidationError:
        return None  # the tool itself reports invalid input
    brief = ctx.conn.execute(
        "SELECT format FROM briefs WHERE id = ?", (submission.brief_id,)
    ).fetchone()
    if brief is None:
        return None
    problems = rights_violations(ctx.conn, submission.plan, brief["format"], ctx.today)
    return "; ".join(problems) + " (H4)" if problems else None


# H5 ----------------------------------------------------------------------------------------


class Audit:
    """One `tool_calls` row per call: tool, input digest, status, duration (NFR-3)."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self.started: dict[str, float] = {}

    def record(self, tool_name: str, tool_input: dict, status: str, started: float) -> None:
        digest = hashlib.sha256(
            json.dumps(tool_input, sort_keys=True, default=str).encode()
        ).hexdigest()
        self.ctx.conn.execute(
            "INSERT INTO tool_calls (run_id, tool, input_digest, status, duration_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                self.ctx.run_id,
                tool_name,
                digest,
                status,
                round((time.monotonic() - started) * 1000),
            ),
        )

    async def before(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        self.started[tool_use_id or ""] = time.monotonic()
        return {}

    def _record(self, input_data: dict, tool_use_id: str | None, status: str) -> None:
        start = self.started.pop(tool_use_id or "", time.monotonic())
        tool_input = input_data.get("tool_input", {})
        self.record(input_data.get("tool_name", "?"), tool_input, status, start)

    async def after(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        response = input_data.get("tool_response")
        failed = isinstance(response, dict) and (
            response.get("is_error") or response.get("isError")
        )
        self._record(input_data, tool_use_id, "error" if failed else "ok")
        return {}

    async def failed(self, input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        self._record(input_data, tool_use_id, "error")
        return {}


# The guard table ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Guard:
    name: str
    matcher: str  # an SDK matcher: one tool name or several joined by "|"
    decide: Decision

    def matches(self, tool_name: str) -> bool:
        return tool_name in self.matcher.split("|")


def guard_table(ctx: ToolContext) -> list[Guard]:
    return [
        Guard("H1", f"{ENGINE}schedule_post", lambda name, args: h1_publish(ctx, args)),
        Guard("H2", "Read", lambda name, args: h2_read_path(args)),
        Guard("H3", H3_MATCHER, lambda name, args: h3_budget(ctx, name, args)),
        Guard("H4", f"{ENGINE}submit_edit_plan", lambda name, args: h4_rights(ctx, args)),
    ]


class GuardedTools:
    """Engine tools called by code (the dry-run director) under the same guards and audit as
    an agent's calls. A guard that raises blocks the call, as `on_failure="block"` does."""

    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx
        self.guards = guard_table(ctx)
        self.audit = Audit(ctx)

    def call(self, name: str, args: dict) -> dict:
        tool_name = f"{ENGINE}{name}"
        started = time.monotonic()
        for guard_ in self.guards:
            if not guard_.matches(tool_name):
                continue
            try:
                reason = guard_.decide(tool_name, args)
            except Exception as exc:  # fail closed
                reason = f"{guard_.name} failed ({exc}); the call is blocked"
            if reason:
                self.audit.record(tool_name, args, "denied", started)
                return {"ok": False, "denied": True, "errors": [reason]}
        try:
            result = call_tool(self.ctx, name, args)
        except Exception:
            self.audit.record(tool_name, args, "error", started)
            raise
        self.audit.record(tool_name, args, "ok" if result["ok"] else "error", started)
        return result


# SDK wiring --------------------------------------------------------------------------------


def guard(decide: Decision):
    """A PreToolUse callback that denies with the decision's reason, or allows."""

    async def callback(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        reason = decide(input_data.get("tool_name", ""), input_data.get("tool_input") or {})
        if reason is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": input_data.get("hook_event_name", "PreToolUse"),
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    return callback


def build_hooks(ctx: ToolContext) -> dict[str, list[HookMatcher]]:
    audit = Audit(ctx)
    guards = [
        HookMatcher(matcher=g.matcher, hooks=[guard(g.decide)], on_failure="block")
        for g in guard_table(ctx)
    ]
    return {
        "PreToolUse": [*guards, HookMatcher(hooks=[audit.before])],
        "PostToolUse": [HookMatcher(hooks=[audit.after])],
        "PostToolUseFailure": [HookMatcher(hooks=[audit.failed])],
    }
