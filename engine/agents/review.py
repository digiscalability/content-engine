"""The QA gate as tools (SRD §6.7): `run_qa_checks` (FR-QA-1, 3, 4, 5, 6, 8), the qa-reviewer's
`get_render_report` and `submit_qa_review` (FR-QA-7), and the copy check `create_draft`
runs on titles, captions and hashtags (FR-QA-8). Self-dedupe (FR-QA-3) and the spec check
(FR-QA-4) joined in P1.3; FR-QA-2 arrives in P2.3. ADRs 0013, 0018.

The deterministic checks run once per render and are stored in `qa_results`. A fail moves
the posts to `qa_failed`, a hold to `qa_held`; only a render whose checks all passed is
reviewed, once, and the review sets `qa_passed`, `qa_held` or `qa_failed`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from engine.agents.context import (
    ToolContext,
    ToolError,
    require_brief,
    require_identity,
    require_providers,
)
from engine.agents.schemas import QAReviewSubmission, ScriptSubmission
from engine.agents.state import advance_posts
from engine.config.models import IdentityConfig
from engine.library.embeddings import to_blob
from engine.produce.editplan import EditPlan
from engine.qa import CheckResult, verdict
from engine.qa.dedupe import recent_scripts, self_dedupe_check
from engine.qa.rights import disclosure_check, rights_check
from engine.qa.spec import SpecRules, spec_check
from engine.qa.text import FactSheet, Text, banned_terms_check, claims_check, post_texts

DETERMINISTIC = (
    "rights",
    "disclosure",
    "banned_topics",
    "claims_about_abbas",
    "self_dedupe",
    "spec",
)
REVIEW = "qa_review"
STATE_FOR = {"fail": "qa_failed", "hold": "qa_held", "pass": "qa_passed"}


class RenderIdInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    render_id: Annotated[int, Field(ge=1)]


@dataclass(frozen=True)
class RenderView:
    render: sqlite3.Row
    plan: EditPlan
    brief: sqlite3.Row
    identity: IdentityConfig
    beats: list[dict]
    words: list[str]
    posts: list[sqlite3.Row]

    @property
    def folder(self) -> Path:
        return Path(self.render["path"]).parent


def render_view(ctx: ToolContext, render_id: int) -> RenderView:
    render = ctx.conn.execute(
        "SELECT r.*, e.brief_id, e.plan FROM renders r "
        "JOIN edit_plans e ON e.id = r.edit_plan_id WHERE r.id = ?",
        (render_id,),
    ).fetchone()
    if render is None:
        raise ToolError(f"no render {render_id}")
    brief = require_brief(ctx, render["brief_id"])
    script_ids = json.loads(render["report"] or "{}").get("scripts")
    if not script_ids:  # fail closed: check what was voiced, never a guess at it
        raise ToolError(f"render {render_id} has no record of its voiced script; render it again")
    beats = []
    for script_id in script_ids:
        script = ctx.conn.execute(
            "SELECT brief_id, body FROM scripts WHERE id = ?", (script_id,)
        ).fetchone()
        if script is None or script["brief_id"] != brief["id"]:
            raise ToolError(f"render {render_id} names script {script_id}, not of its brief")
        body = ScriptSubmission.model_validate_json(script["body"])
        beats += [b.model_dump() for b in body.beats]
    words_file = Path(render["path"]).parent / "words.json"
    words = (
        [w["text"] for w in json.loads(words_file.read_text(encoding="utf-8"))]
        if words_file.is_file()
        else []
    )
    posts = ctx.conn.execute(
        "SELECT p.*, c.platform FROM posts p JOIN channels c ON c.id = p.channel_id "
        "WHERE p.brief_id = ? ORDER BY p.id",
        (brief["id"],),
    ).fetchall()
    return RenderView(
        render=render,
        plan=EditPlan.model_validate_json(render["plan"]),
        brief=brief,
        identity=require_identity(ctx, brief["identity_id"]),
        beats=beats,
        words=words,
        posts=posts,
    )


def _fact_sheet(ctx: ToolContext) -> FactSheet | None:
    path = ctx.config.engine.qa.fact_sheet
    return FactSheet.load(Path(path)) if path else None


def text_checks(ctx: ToolContext, identity: IdentityConfig, texts: Sequence[Text]):
    return [
        banned_terms_check(texts, identity.qa.banned_terms),
        claims_check(texts, _fact_sheet(ctx), identity.qa.self_reference_terms,
                     identity.qa.never_terms),
    ]  # fmt: skip


def stored_checks(ctx: ToolContext, render_id: int) -> dict[str, CheckResult]:
    rows = ctx.conn.execute(
        "SELECT check_name, result, details FROM qa_results WHERE render_id = ? ORDER BY id",
        (render_id,),
    )
    return {
        r["check_name"]: CheckResult(
            r["check_name"], r["result"], json.loads(r["details"] or "{}").get("reasons", [])
        )
        for r in rows
    }


def _as_dict(result: CheckResult) -> dict:
    return {"name": result.name, "result": result.result, "reasons": result.reasons}


def require_qa_passed(ctx: ToolContext, render_id: int) -> None:
    """Every deterministic check and the review passed for this render (§6.7)."""
    stored = stored_checks(ctx, render_id)
    missing = [n for n in (*DETERMINISTIC, REVIEW) if n not in stored]
    if missing:
        raise ToolError(
            f"render {render_id} has no {', '.join(missing)} result; call run_qa_checks and "
            "get the qa-reviewer's verdict first"
        )
    unpassed = [f"{n} ({stored[n].result})" for n in (*DETERMINISTIC, REVIEW)
                if stored[n].result != "pass"]  # fmt: skip
    if unpassed:
        raise ToolError(f"render {render_id} did not pass {', '.join(unpassed)}")


# run_qa_checks ------------------------------------------------------------------------------


def _self_dedupe(ctx: ToolContext, view: RenderView, render_id: int) -> CheckResult:
    """Embed the voiced script, store it on the render, compare it (FR-QA-3)."""
    limits = ctx.config.engine.qa.self_dedupe
    spoken = " ".join(beat["text"] for beat in view.beats)
    (vector,) = require_providers(ctx).embedder.embed([spoken])
    ctx.conn.execute(
        "UPDATE renders SET script_embedding = ? WHERE id = ?", (to_blob(vector), render_id)
    )
    earlier = recent_scripts(ctx.conn, view.identity.id, view.brief["id"], limits.lookback_posts)
    return self_dedupe_check(vector, earlier, limits.script_cosine_max)


def _spec(ctx: ToolContext, view: RenderView) -> CheckResult:
    """FR-QA-4 on what the render report measured."""
    report = json.loads(view.render["report"]) if view.render["report"] else None
    return spec_check(
        report,
        SpecRules.from_engine(ctx.config.engine, view.brief["format"]),
        [post["platform"] for post in view.posts],
        speech=bool(view.plan.audio.voice),
        captions=view.plan.captions is not None,
        silent_voice=getattr(require_providers(ctx).voice, "silent", False),
    )


def _run_check(ctx: ToolContext, view: RenderView, render_id: int, name: str) -> list:
    """The check `name`; the two text checks run together, under `banned_topics`."""
    if name == "rights":
        return [rights_check(ctx.conn, view.plan, view.brief["format"], ctx.today)]
    if name == "disclosure":
        return [disclosure_check(ctx.conn, view.plan, view.posts)]
    if name == "self_dedupe":
        return [_self_dedupe(ctx, view, render_id)]
    if name == "spec":
        return [_spec(ctx, view)]
    if name in ("banned_topics", "claims_about_abbas"):
        return text_checks(ctx, view.identity, post_texts(view.beats, view.words))
    raise ValueError(f"unknown check {name}")


def run_qa_checks(ctx: ToolContext, args: RenderIdInput) -> dict:
    view = render_view(ctx, args.render_id)
    stored = stored_checks(ctx, args.render_id)
    fresh: dict[str, CheckResult] = {}
    for name in DETERMINISTIC:  # each runs once per render; older renders get only new ones
        if name not in stored and name not in fresh:
            for result in _run_check(ctx, view, args.render_id, name):
                if result.name not in stored:
                    fresh[result.name] = result
    for result in fresh.values():
        ctx.conn.execute(
            "INSERT INTO qa_results (render_id, check_name, result, details) "
            "VALUES (?, ?, ?, ?)",
            (args.render_id, result.name, result.result,
             json.dumps({"reasons": result.reasons})),
        )  # fmt: skip
    results = [stored.get(name) or fresh[name] for name in DETERMINISTIC]
    worst = verdict("qa", [r.name for r in results if r.result == "fail"],
                    [r.name for r in results if r.result == "hold"])  # fmt: skip
    if worst.result != "pass":
        advance_posts(ctx.conn, view.brief["id"], STATE_FOR[worst.result], of_render=args.render_id)
    return {
        "passed": worst.result == "pass",
        "checks": [_as_dict(r) for r in results],
        "next": "ask the qa-reviewer" if worst.result == "pass" else f"post is {worst.result}",
    }


# get_render_report ----------------------------------------------------------------------------


def get_render_report(ctx: ToolContext, args: RenderIdInput) -> dict:
    view = render_view(ctx, args.render_id)
    report = json.loads(view.render["report"]) if view.render["report"] else None
    return {
        "render_id": args.render_id,
        "identity": view.identity.id,
        "format": view.brief["format"],
        "brief": {
            k: view.brief[k]
            for k in ("topic", "angle", "hook", "cta", "difference_note", "exploration")
        },
        "script": view.beats,
        "captions": " ".join(view.words),
        "report": report,
        "checks": [_as_dict(r) for r in stored_checks(ctx, args.render_id).values()],
    }


# submit_qa_review -----------------------------------------------------------------------------


def submit_qa_review(ctx: ToolContext, review: QAReviewSubmission) -> dict:
    view = render_view(ctx, review.render_id)
    stored = stored_checks(ctx, review.render_id)
    if REVIEW in stored:
        raise ToolError(
            f"render {review.render_id} was already reviewed ({stored[REVIEW].result}); "
            "a hold goes to Abbas"
        )
    missing = [name for name in DETERMINISTIC if name not in stored]
    if missing:
        raise ToolError(
            f"call run_qa_checks first: the reviewer judges a render only after every "
            f"deterministic check passed (§6.7); missing {', '.join(missing)}"
        )
    unpassed = [f"{n} ({stored[n].result})" for n in DETERMINISTIC if stored[n].result != "pass"]
    if unpassed:
        raise ToolError(f"render {review.render_id} did not pass {', '.join(unpassed)}")
    cur = ctx.conn.execute(
        "INSERT INTO qa_results (render_id, check_name, result, details) VALUES (?, ?, ?, ?)",
        (
            review.render_id,
            REVIEW,
            review.verdict,
            json.dumps(
                {
                    "reasons": review.reasons,
                    "adds": review.adds,
                    "fits_identity": review.fits_identity,
                }
            ),
        ),  # fmt: skip
    )
    advance_posts(ctx.conn, view.brief["id"], STATE_FOR[review.verdict], of_render=review.render_id)
    return {"qa_result_id": cur.lastrowid, "state": STATE_FOR[review.verdict]}
