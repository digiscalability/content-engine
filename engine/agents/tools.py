"""The engine's agent tools (SRD §5.4), as plain Python first.

Each tool is a `ToolSpec`: a name, a description the model reads, a JSON input schema
generated from a pydantic model, and a handler that takes a `ToolContext` and the
validated input. `call_tool()` validates and runs one; `options.py` wraps them as the
in-process MCP server `engine` (tool names `mcp__engine__<name>`). Read tools return
views; submit tools validate against the schemas and store, returning an id or errors.
Action tools (voice, avatar, render, draft) live in `actions.py`; QA checks arrive in P0.7.3.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from engine.agents import actions, review
from engine.agents.context import (
    Providers,
    ToolContext,
    ToolError,
    require_alive,
    require_brief,
    require_identity,
)
from engine.agents.schemas import (
    FORMAT_SECONDS,
    BriefSubmission,
    EditPlanSubmission,
    MomentScoresSubmission,
    QAReviewSubmission,
    ScriptSubmission,
    allowed_tiers,
)
from engine.agents.state import advance_posts, plan_posts
from engine.config.models import Format, IdentityConfig
from engine.library.search import search, search_music
from engine.library.stock_fetch import queue_request
from engine.ops.budget import budget_lines
from engine.plan.playbook import Playbook, PlaybookError, load_playbook
from engine.plan.topics import WINDOW, exploration_due, next_format, topic_candidates
from engine.produce.voice import engine_characters
from engine.qa.rights import rights_violations
from engine.qa.text import FactSheet, Text, banned_terms_check, claims_check


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    model: type[BaseModel]
    handler: Callable[[ToolContext, Any], dict]
    kind: Literal["read", "write", "action"]

    @property
    def input_schema(self) -> dict:
        return self.model.model_json_schema()


__all__ = ["TOOLS", "Providers", "ToolContext", "ToolError", "ToolSpec", "call_tool"]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoInput(_Input):
    pass


class IdentityInput(_Input):
    identity: str


class TopicInput(_Input):
    identity: str
    limit: int = Field(default=5, ge=1, le=50)


Bpm = Annotated[int, Field(ge=30, le=250)]


class SearchInput(_Input):
    query: str = Field(default="", description="What the cutaway should show, in words.")
    format: Format
    limit: int = Field(default=10, ge=1, le=50)
    brief_id: int | None = Field(
        default=None,
        ge=1,
        description="The brief this beat is for. When the best match is weak, the query is "
        "queued and stock footage for it is fetched overnight (FR-ING-5).",
    )
    kind: Literal["footage", "music"] = Field(
        default="footage",
        description="footage: cutaway clips; music: tracks from the music pool for the bed.",
    )
    tempo_bpm: tuple[Bpm, Bpm] | None = Field(
        default=None, description="music: the look's tempo range, [min, max] bpm."
    )
    energy: Literal["low", "medium", "high"] | None = Field(
        default=None, description="music: the look's energy."
    )

    @model_validator(mode="after")
    def _tempo_range(self) -> SearchInput:
        if self.tempo_bpm and self.tempo_bpm[0] > self.tempo_bpm[1]:
            raise ValueError("tempo_bpm is [min, max] with min <= max")
        return self


def _within_format(fmt: str, seconds: float, what: str) -> None:
    low, high = FORMAT_SECONDS[fmt]
    if not low <= seconds <= high:
        raise ToolError(f"{what} runs {seconds:g} s; format {fmt} must be {low}-{high} s (§4.1)")


def _week_start(ctx: ToolContext) -> date:
    return ctx.today - timedelta(days=ctx.today.weekday())  # Monday


def _fact_sheet(ctx: ToolContext) -> FactSheet | None:
    sheet = ctx.config.engine.qa.fact_sheet
    return FactSheet.load(Path(sheet)) if sheet else None


def _playbook(ctx: ToolContext, identity: IdentityConfig) -> Playbook:
    try:
        return load_playbook(ctx.config_dir / identity.playbook)
    except PlaybookError as exc:
        raise ToolError(f"{identity.id}: {exc}") from exc


def _text_checks(identity: IdentityConfig, facts: FactSheet | None, texts: list[Text]) -> list:
    """The banned-term and claims checks (FR-QA-6, FR-QA-8) a script must pass, run early
    on planning text so a bad topic costs nothing."""
    qa = identity.qa
    return [
        banned_terms_check(texts, qa.banned_terms),
        claims_check(texts, facts, qa.self_reference_terms, qa.never_terms),
    ]


def _topic_view(topic) -> dict:
    return {"topic": topic.text, "pillar": topic.pillar, "explore": topic.explore}


# Read tools ------------------------------------------------------------------------------


def get_state(ctx: ToolContext, _: NoInput) -> dict:
    week_start = _week_start(ctx)
    identities = []
    for row in ctx.conn.execute("SELECT * FROM identities ORDER BY id"):
        config = ctx.config.identities.get(row["id"])
        if config is None:
            continue
        this_week = ctx.conn.execute(
            "SELECT COUNT(*) FROM briefs WHERE identity_id = ? AND date(created_at) >= ?",
            (row["id"], week_start.isoformat()),
        ).fetchone()[0]
        identities.append(
            {
                "id": row["id"],
                "autonomy_level": row["autonomy_level"],
                "kill_flag": bool(row["kill_flag"]),
                "due": not row["kill_flag"] and this_week < config.cadence.posts_per_week,
                "briefs_this_week": this_week,
                "formats": dict(config.formats),
                "channels": [c.platform for c in config.channels],
            }
        )
    queue = [
        dict(r)
        for r in ctx.conn.execute(
            "SELECT p.id AS post_id, p.brief_id, c.platform, p.state FROM posts p "
            "JOIN channels c ON c.id = p.channel_id "
            "WHERE p.state NOT IN ('published', 'rejected', 'failed') ORDER BY p.id"
        )
    ]
    return {"date": ctx.today.isoformat(), "identities": identities, "queue": queue}


def get_playbook(ctx: ToolContext, args: IdentityInput) -> dict:
    identity = require_identity(ctx, args.identity)
    book = _playbook(ctx, identity)
    facts = _fact_sheet(ctx)
    never = [*identity.qa.never_terms, *(facts.never_terms if facts else [])]
    return {
        "identity": identity.id,
        "playbook": book.raw,
        "pillars": book.pillars,
        "banned": book.banned,
        "topic_bank": [_topic_view(t) for t in book.topics],
        "learned": book.learned,
        "never_terms": never,
    }


def get_topic_candidates(ctx: ToolContext, args: TopicInput) -> dict:
    """FR-PLN-1, FR-PLN-2: the next post's format, whether it explores, and bank topics."""
    identity = require_identity(ctx, args.identity)
    book = _playbook(ctx, identity)
    facts = _fact_sheet(ctx)
    usable, skipped = [], []
    for topic in book.topics:
        checks = _text_checks(identity, facts, [("topic bank", topic.text)])
        reasons = [r for c in checks if c.result != "pass" for r in c.reasons]
        if reasons:
            skipped.append({"topic": topic.text, "reason": "; ".join(reasons)})
        else:
            usable.append(topic)
    rows = ctx.conn.execute(
        "SELECT topic, format, exploration, date(created_at) AS day FROM briefs "
        "WHERE identity_id = ? ORDER BY created_at DESC, id DESC",
        (identity.id,),
    ).fetchall()
    week = [r for r in rows if r["day"] >= _week_start(ctx).isoformat()]
    explored = sum(r["exploration"] for r in week)
    explore = exploration_due(identity.exploration_share, len(week), explored)
    bank = replace(book, topics=usable)
    history = [r["topic"] for r in rows]
    offered = topic_candidates(bank, history, explore, args.limit)
    note = "Copy a candidate's topic into submit_brief as written, and set exploration as given."
    if explore and not offered:
        offered = topic_candidates(bank, history, False, args.limit)
        note += (
            " No explore topic is left in the bank: explore through a new format or hook "
            "pattern on one of these topics instead (FR-PLN-2)."
        )
    if not offered:
        note += (
            f" Every bank topic was used in the last {WINDOW} briefs: pick a new topic within "
            "a pillar, clear of the banned list, until Abbas adds more."
        )
    return {
        "identity": identity.id,
        "format": next_format(identity.formats, [r["format"] for r in rows[:WINDOW]]),
        "exploration": explore,
        "exploration_share": identity.exploration_share,
        "briefs_this_week": len(week),
        "explored_this_week": explored,
        "candidates": [_topic_view(t) for t in offered],
        "skipped": skipped,
        "note": note,
    }


def get_style_profile(ctx: ToolContext, args: IdentityInput) -> dict:
    require_identity(ctx, args.identity)
    row = ctx.conn.execute(
        "SELECT metrics, palette, ref_count, measured_at FROM style_profiles "
        "WHERE identity_id = ? ORDER BY measured_at DESC LIMIT 1",
        (args.identity,),
    ).fetchone()
    if row is None:
        return {"profile": None, "note": "no style profile yet (P0.8)"}
    return {
        "profile": json.loads(row["metrics"]),
        "palette": json.loads(row["palette"] or "null"),
        "ref_count": row["ref_count"],
        "measured_at": row["measured_at"],
    }


def get_trends(ctx: ToolContext, args: IdentityInput) -> dict:
    require_identity(ctx, args.identity)
    since = (ctx.today - timedelta(days=7)).isoformat()
    rows = ctx.conn.execute(
        "SELECT source, query, score, date FROM trends WHERE identity_id = ? AND date >= ? "
        "ORDER BY score DESC LIMIT 50",
        (args.identity, since),
    )
    return {"trends": [dict(r) for r in rows]}


def budget_status(ctx: ToolContext, _: NoInput) -> dict:
    budgets = ctx.config.engine.budgets
    lines = {line.vendor: line for line in budget_lines(ctx.conn, ctx.config, ctx.month)}
    return {
        "month": ctx.month,
        "claude_usd_month": round(lines["anthropic"].used, 4),
        "claude_monthly_cap_usd": budgets.claude.monthly_cap_usd,
        "run_budget_usd": budgets.claude.max_budget_usd.daily,
        "elevenlabs_chars_month": engine_characters(ctx.conn, ctx.month),
        "elevenlabs_cap": budgets.elevenlabs.monthly_char_cap,
        "elevenlabs_floor": budgets.elevenlabs.account_floor_chars,
        "elevenlabs_balance": ctx.voice_balance.remaining if ctx.voice_balance else None,
        "heygen_credits_month": round(lines["heygen"].used, 3),
        "heygen_credit_cap": budgets.heygen.monthly_credit_cap,
        "stops": [line.label for line in lines.values() if line.stopped],  # NFR-4 hard stops
    }


def search_library(ctx: ToolContext, args: SearchInput) -> dict:
    """Rights-clean, people-clean, licence-valid footage for a format, ranked by meaning
    (FR-PRD-4, FR-UND-3, §8.3). A brief's weak beat query is queued for stock (FR-ING-5)."""
    if args.brief_id is not None:
        require_brief(ctx, args.brief_id)
    if args.kind == "music":
        return search_music(ctx.conn, allowed_tiers(args.format), args.query, ctx.today,
                            args.limit, args.tempo_bpm, args.energy)  # fmt: skip
    result = search(ctx.conn, allowed_tiers(args.format), args.query, ctx.today, args.limit,
                    ctx.search_models)  # fmt: skip
    if args.brief_id is not None:
        result["stock_request"] = queue_request(ctx.conn, ctx.config, args.brief_id,
                                                args.query, args.format, result)  # fmt: skip
    return result


# Submit tools -----------------------------------------------------------------------------


def submit_brief(ctx: ToolContext, brief: BriefSubmission) -> dict:
    identity = require_identity(ctx, brief.identity_id)
    require_alive(ctx, identity.id)
    if identity.formats.get(brief.format, 0) <= 0:
        raise ToolError(
            f"{identity.id} does not use format {brief.format} (its formats: "
            f"{', '.join(identity.formats)})"
        )
    texts = [(f"brief {f}", getattr(brief, f)) for f in ("topic", "angle", "hook", "cta")]
    failed = [c for c in _text_checks(identity, _fact_sheet(ctx), texts) if c.result == "fail"]
    if failed:
        raise ToolError("; ".join(r for c in failed for r in c.reasons))
    cur = ctx.conn.execute(
        "INSERT INTO briefs (identity_id, format, topic, angle, hook, cta, source_moments, "
        "difference_note, exploration) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            brief.identity_id,
            brief.format,
            brief.topic,
            brief.angle,
            brief.hook,
            brief.cta,
            json.dumps(brief.source_moments),
            brief.difference_note,
            int(brief.exploration),
        ),
    )
    plan_posts(ctx.conn, cur.lastrowid, identity.id, brief.format)
    return {"brief_id": cur.lastrowid}


def _next_version(ctx: ToolContext, table: str, brief_id: int) -> int:
    row = ctx.conn.execute(
        f"SELECT COALESCE(MAX(version), 0) FROM {table} WHERE brief_id = ?", (brief_id,)
    ).fetchone()
    return int(row[0]) + 1


def submit_script(ctx: ToolContext, script: ScriptSubmission) -> dict:
    brief = require_brief(ctx, script.brief_id)
    _within_format(brief["format"], script.duration, "the script")
    version = _next_version(ctx, "scripts", script.brief_id)
    cur = ctx.conn.execute(
        "INSERT INTO scripts (brief_id, version, body) VALUES (?, ?, ?)",
        (script.brief_id, version, script.model_dump_json()),
    )
    advance_posts(ctx.conn, script.brief_id, "scripted")
    return {
        "script_id": cur.lastrowid,
        "version": version,
        "duration": script.duration,
        "words": script.words,
    }


def submit_edit_plan(ctx: ToolContext, submission: EditPlanSubmission) -> dict:
    brief = require_brief(ctx, submission.brief_id)
    plan = submission.plan
    _within_format(brief["format"], plan.duration, "the edit plan")
    problems = rights_violations(ctx.conn, plan, brief["format"], ctx.today)
    if problems:
        raise ToolError("; ".join(problems))
    version = _next_version(ctx, "edit_plans", submission.brief_id)
    cur = ctx.conn.execute(
        "INSERT INTO edit_plans (brief_id, version, plan) VALUES (?, ?, ?)",
        (submission.brief_id, version, plan.model_dump_json(by_alias=True)),
    )
    return {"edit_plan_id": cur.lastrowid, "version": version, "duration": plan.duration}


def submit_moment_scores(ctx: ToolContext, submission: MomentScoresSubmission) -> dict:
    ids = []
    for s in submission.scores:
        cur = ctx.conn.execute(
            "INSERT INTO moments (asset_id, start_s, end_s, text, hook_score, standalone_score, "
            "scorer_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                s.asset_id,
                s.start_s,
                s.end_s,
                s.text,
                s.hook_score,
                s.standalone_score,
                submission.scorer_version,
            ),
        )
        ids.append(cur.lastrowid)
    return {"moment_ids": ids}


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "get_state",
            "Today's identities (due, autonomy level, kill flag) and the post queue.",
            NoInput,
            get_state,
            "read",
        ),
        ToolSpec(
            "get_playbook",
            "An identity's playbook: Abbas's rules (voice, pillars, banned topics, "
            "disclosure, personal facts, topic bank) and the learned section.",
            IdentityInput,
            get_playbook,
            "read",
        ),
        ToolSpec(
            "get_topic_candidates",
            "The next post's plan for an identity: the format furthest below its share, "
            "whether this post explores (the identity's exploration share of each week), and "
            "bank topics not used in the last 20 briefs, best first.",
            TopicInput,
            get_topic_candidates,
            "read",
        ),
        ToolSpec(
            "get_style_profile",
            "Numbers measured from the identity's reference shorts.",
            IdentityInput,
            get_style_profile,
            "read",
        ),
        ToolSpec(
            "get_trends",
            "The identity's trend rows from the last 7 days.",
            IdentityInput,
            get_trends,
            "read",
        ),
        ToolSpec(
            "budget_status",
            "This month's Claude, ElevenLabs and HeyGen spend against their caps, and any "
            "vendor already at its cap (a hard stop).",
            NoInput,
            budget_status,
            "read",
        ),
        ToolSpec(
            "search_library",
            "Footage a format may use (rights-clean, no unreleased faces, licences valid), "
            "ranked by meaning: `results` are scenes whose picture fits the query (one per "
            "clip), `spoken` are transcript passages that say it.",
            SearchInput,
            search_library,
            "read",
        ),
        ToolSpec(
            "submit_brief",
            "Store the brief for one post. The difference note must say "
            "why it differs from the identity's last 20 posts.",
            BriefSubmission,
            submit_brief,
            "write",
        ),
        ToolSpec(
            "submit_script",
            "Store the script for a brief as timed beats (at most 3 "
            "spoken words a second; the total fits the format).",
            ScriptSubmission,
            submit_script,
            "write",
        ),
        ToolSpec(
            "submit_edit_plan",
            "Store the edit plan for a brief. Every shot must be rights-clean for the format.",
            EditPlanSubmission,
            submit_edit_plan,
            "write",
        ),
        ToolSpec(
            "submit_moment_scores",
            "Store hook and standalone scores for candidate moments.",
            MomentScoresSubmission,
            submit_moment_scores,
            "write",
        ),
        ToolSpec(
            "synthesize_voice",
            "Voice a script with the identity's ElevenLabs clone (WAV 48 kHz). Charged once "
            "per script; returns the voice asset id and its duration.",
            actions.ScriptInput,
            actions.synthesize_voice,
            "action",
        ),
        ToolSpec(
            "request_avatar",
            "Ask for the avatar video of a voiced script. Answers ready (with the video asset "
            "id) or waiting (Abbas renders it; the post waits for him).",
            actions.AvatarInput,
            actions.request_avatar,
            "action",
        ),
        ToolSpec(
            "render_post",
            "Render an edit plan to the final MP4: mix, captions from the mix, encode. "
            "Returns the render id.",
            actions.RenderInput,
            actions.render_post,
            "action",
        ),
        ToolSpec(
            "create_draft",
            "Upload a render that passed QA and make one Postiz draft per channel, with its "
            "title, caption and hashtags. Never schedules.",
            actions.DraftInput,
            actions.create_draft,
            "action",
        ),
        ToolSpec(
            "run_qa_checks",
            "Run the deterministic QA checks on a render: rights, AI disclosure, banned "
            "topics, claims about Abbas. Only a render that passes them all goes to review.",
            review.RenderIdInput,
            review.run_qa_checks,
            "action",
        ),
        ToolSpec(
            "get_render_report",
            "What a render contains: brief, script, captions, durations per rights tier, "
            "people flags, loudness, resolution, and the QA checks so far.",
            review.RenderIdInput,
            review.get_render_report,
            "read",
        ),
        ToolSpec(
            "submit_qa_review",
            "Store the review of a render that passed every check: pass, hold or fail (hold "
            "and fail need reasons). Once per render.",
            QAReviewSubmission,
            review.submit_qa_review,
            "write",
        ),
    )
}


def call_tool(ctx: ToolContext, name: str, args: dict) -> dict:
    """Validate and run one tool; failures come back as `{"ok": False, "errors": [...]}`."""
    spec = TOOLS[name]
    try:
        parsed = spec.model.model_validate(args)
        return {"ok": True, **spec.handler(ctx, parsed)}
    except ValidationError as exc:
        errors = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        return {"ok": False, "errors": errors}
    except ToolError as exc:
        return {"ok": False, "errors": [str(exc)]}
