"""Action tools (SRD §5.4): voice, avatar, render and draft, each behind `Providers`.

Each one moves the brief's posts along §10 and is safe to call again (NFR-1): a voiced
script is never charged twice, an avatar job is requested once and then polled, a drafted
post is never drafted again. Provider failures come back as `ToolError`s the agent reads.
Choices are recorded in ADR 0012.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from engine.agents.context import (
    ToolContext,
    ToolError,
    require_brief,
    require_identity,
    require_providers,
)
from engine.agents.review import require_qa_passed, text_checks
from engine.agents.schemas import ScriptSubmission
from engine.agents.state import advance_posts, fail_posts
from engine.config.models import Platform
from engine.library.assets import register_generated
from engine.library.transcribe import refine_starts
from engine.produce.avatar import AvatarError, AvatarRequest
from engine.produce.captions import render_captions, voice_onsets
from engine.produce.editplan import EditPlan
from engine.produce.exports import ExportMedia, plan_timeline, write_fcpxml, write_otio
from engine.produce.ffmpeg import probe, wav_duration
from engine.produce.render_ffmpeg import RenderError, RenderSources, mix_audio, render
from engine.produce.report import AssetFacts, build_render_report, measure_loudness
from engine.produce.voice import VoiceError, VoiceRequest, synthesize_batch
from engine.publish.postiz import PostizError, draft_post, tiktok_settings, youtube_settings
from engine.qa.text import post_texts

Id = Annotated[int, Field(ge=1)]
Hashtag = Annotated[str, StringConstraints(pattern=r"^#\w+$")]
DRAFTED = ("drafted", "uploaded_private", "scheduled", "published")


class ActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScriptInput(ActionInput):
    script_id: Id


class AvatarInput(ActionInput):
    brief_id: Id
    script_id: Id


class RenderInput(ActionInput):
    edit_plan_id: Id


class PostCopy(ActionInput):
    platform: Platform
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    caption: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    hashtags: list[Hashtag] = []


class DraftInput(ActionInput):
    render_id: Id
    posts: list[PostCopy] = Field(min_length=1, description="One entry per channel platform.")


def _script(ctx: ToolContext, script_id: int) -> tuple[sqlite3.Row, ScriptSubmission]:
    row = ctx.conn.execute("SELECT * FROM scripts WHERE id = ?", (script_id,)).fetchone()
    if row is None:
        raise ToolError(f"no script {script_id}")
    return row, ScriptSubmission.model_validate_json(row["body"])


def _voice_file(ctx: ToolContext, script_id: int) -> Path:
    return ctx.work_dir / "voice" / f"script-{script_id}.wav"


# synthesize_voice ---------------------------------------------------------------------------


def synthesize_voice(ctx: ToolContext, args: ScriptInput) -> dict:
    providers = require_providers(ctx)
    row, script = _script(ctx, args.script_id)
    brief = require_brief(ctx, row["brief_id"])
    identity = require_identity(ctx, brief["identity_id"])
    if not identity.voice_id:
        raise ToolError(f"{identity.id} has no voice_id yet (G0.4: the ElevenLabs clone)")
    request = VoiceRequest(
        script_id=args.script_id, text=script.spoken_text, voice_id=identity.voice_id
    )
    budget = ctx.config.engine.budgets.elevenlabs
    try:
        outputs = synthesize_batch(
            [request],
            providers.voice,
            ctx.conn,
            ctx.work_dir / "voice",
            budget.monthly_char_cap,
            budget.account_floor_chars,
            ctx.month,
        )
    except VoiceError as exc:
        raise ToolError(str(exc)) from exc
    wav = outputs[args.script_id]
    duration = wav_duration(wav)
    asset_id = register_generated(ctx.conn, wav, "audio", duration)
    advance_posts(ctx.conn, brief["id"], "voiced")
    return {
        "asset_id": asset_id,
        "duration": duration,
        "characters": request.characters,
        "path": str(wav),
    }


# request_avatar -----------------------------------------------------------------------------


def request_avatar(ctx: ToolContext, args: AvatarInput) -> dict:
    providers = require_providers(ctx)
    brief = require_brief(ctx, args.brief_id)
    row, script = _script(ctx, args.script_id)
    if row["brief_id"] != args.brief_id:
        raise ToolError(f"script {args.script_id} belongs to brief {row['brief_id']}")
    identity = require_identity(ctx, brief["identity_id"])
    if not identity.avatar_id:
        raise ToolError(f"{identity.id} has no avatar_id yet (G0.5: the HeyGen avatar)")
    audio = _voice_file(ctx, args.script_id)
    if not audio.is_file():
        raise ToolError(f"script {args.script_id} has no voice yet; call synthesize_voice first")
    request = AvatarRequest(args.brief_id, audio, identity.avatar_id, script.spoken_text)
    try:
        status = providers.avatar.request(request)
    except AvatarError as exc:
        raise ToolError(str(exc)) from exc
    if status.state == "ready" and status.video is not None:
        info = probe(status.video, providers.run)
        asset_id = register_generated(
            ctx.conn, status.video, "video", info.duration, info.width, info.height, info.fps
        )
        advance_posts(ctx.conn, args.brief_id, "avatar_ready")
        return {"state": "ready", "asset_id": asset_id, "duration": info.duration}
    if status.state == "waiting":
        advance_posts(ctx.conn, args.brief_id, "avatar_waiting")
        return {"state": "waiting", "detail": status.detail, "needs_abbas": True}
    reason = f"avatar {status.state}: {status.detail}"
    fail_posts(ctx.conn, args.brief_id, reason)  # FR-PRD-3: dropped after 48 h
    raise ToolError(reason)


# render_post --------------------------------------------------------------------------------


def _sources(ctx: ToolContext, plan: EditPlan) -> RenderSources:
    ids = {s.asset_id for s in plan.timeline} | {v.asset_id for v in plan.audio.voice}
    ids |= {h.asset_id for h in plan.audio.sfx}
    if plan.audio.music:
        ids.add(plan.audio.music.asset_id)
    paths = {}
    for asset_id in sorted(ids):
        row = ctx.conn.execute("SELECT path FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None or not Path(row["path"]).is_file():
            raise ToolError(f"asset {asset_id} has no file on disk")
        paths[asset_id] = Path(row["path"])
    return RenderSources(assets=paths)


def _facts(ctx: ToolContext, plan: EditPlan) -> dict[int, AssetFacts]:
    ids = {s.asset_id for s in plan.timeline}
    rows = ctx.conn.execute(
        f"SELECT id, rights_type, people_release, media_kind FROM assets "
        f"WHERE id IN ({','.join('?' * len(ids))})",
        sorted(ids),
    )
    return {
        r["id"]: AssetFacts(r["rights_type"], r["people_release"], r["media_kind"]) for r in rows
    }


def _export(
    ctx: ToolContext, plan: EditPlan, sources: RenderSources, name: str, folder: Path, fps: int
) -> dict[str, Path]:
    """OTIO and FCPXML of the edit beside the MP4, for hand polish in Resolve (C7, §7.4)."""
    facts = {
        r["id"]: r
        for r in ctx.conn.execute(
            f"SELECT id, duration, width, height FROM assets "
            f"WHERE id IN ({','.join('?' * len(sources.assets))})",
            list(sources.assets),
        )
    }
    media = ExportMedia(
        assets=sources.assets,
        durations={i: r["duration"] for i, r in facts.items() if r["duration"]},
        sizes={i: (r["width"], r["height"]) for i, r in facts.items() if r["width"]},
        captions=sources.captions,
        overlays=sources.overlays,
    )
    return {
        "otio": write_otio(plan_timeline(plan, media, name, fps), folder / f"{name}.otio"),
        "fcpxml": write_fcpxml(plan, media, name, fps, folder / f"{name}.fcpxml"),
    }


def render_post(ctx: ToolContext, args: RenderInput) -> dict:
    providers = require_providers(ctx)
    row = ctx.conn.execute("SELECT * FROM edit_plans WHERE id = ?", (args.edit_plan_id,)).fetchone()
    if row is None:
        raise ToolError(f"no edit plan {args.edit_plan_id}")
    brief = require_brief(ctx, row["brief_id"])
    plan = EditPlan.model_validate_json(row["plan"])
    if plan.placed_overlays():
        raise ToolError(
            "overlay blocks come from the identity's approved look (P0.8); "
            "until then plans carry captions only"
        )
    script = ctx.conn.execute(
        "SELECT id FROM scripts WHERE brief_id = ? ORDER BY version DESC LIMIT 1", (brief["id"],)
    ).fetchone()
    if plan.captions and script is None:
        raise ToolError(f"brief {brief['id']} has no script to caption")
    sources = _sources(ctx, plan)
    work = ctx.work_dir / f"render-{args.edit_plan_id}"
    name = f"{brief['identity_id']}-{brief['id']}-{args.edit_plan_id}"
    folder = ctx.renders_dir / name  # the MP4, its report and exports, together (FR-PRD-7)
    output = folder / f"{name}.mp4"
    settings = providers.render_settings
    words = []
    try:
        mix = mix_audio(plan, sources, work, settings, providers.run)
        if plan.captions:
            spoken = _script(ctx, script["id"])[1].spoken_text
            heard = providers.transcriber(spoken).transcribe(mix).words
            words = refine_starts(heard, voice_onsets(plan, sources, providers.run))
            captions = render_captions(
                words, plan.duration, work / "captions", work / "captions.mov", providers.overlay
            )
            sources = replace(sources, captions=captions)
        result = render(plan, sources, output, work, settings, providers.run, mix=mix)
        info = probe(output, providers.run)
        loudness = measure_loudness(output, providers.run)
    except (RenderError, RuntimeError) as exc:  # ffmpeg, HyperFrames, silencedetect
        raise ToolError(f"render of edit plan {args.edit_plan_id} failed: {exc}") from exc
    timings = [{"text": w.text, "start": w.start, "end": w.end} for w in words]
    (folder / "words.json").write_text(json.dumps(timings, indent=1), encoding="utf-8")
    render_id = ctx.conn.execute(
        "INSERT INTO renders (edit_plan_id, path) VALUES (?, ?)", (args.edit_plan_id, str(output))
    ).lastrowid
    loudness_target = ctx.config.engine.loudness
    report = {
        "render_id": render_id,
        "edit_plan_id": args.edit_plan_id,
        "brief_id": brief["id"],
        "identity": brief["identity_id"],
        "format": brief["format"],
        "file": output.name,
        **build_render_report(
            plan, _facts(ctx, plan), info, loudness, words,
            (float(loudness_target.integrated_lufs), float(loudness_target.true_peak_dbtp)),
        ),
    }  # fmt: skip
    exports = _export(ctx, plan, sources, name, folder, settings.fps)
    report["exports"] = {kind: path.name for kind, path in exports.items()}
    text = json.dumps(report, indent=1)
    (folder / "render_report.json").write_text(text, encoding="utf-8")
    ctx.conn.execute("UPDATE renders SET report = ? WHERE id = ?", (text, render_id))
    advance_posts(ctx.conn, brief["id"], "rendered", render_id=render_id)
    ctx.renders[brief["identity_id"]] = ctx.renders.get(brief["identity_id"], 0) + 1
    return {
        "render_id": render_id,
        "path": str(result.path),
        "report": str(folder / "render_report.json"),
        "exports": {kind: str(path) for kind, path in exports.items()},
        "duration": result.duration,
        "captions": bool(plan.captions),
    }


# create_draft -------------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_slot(ctx: ToolContext, channel_id: int, gap_hours: float) -> datetime:
    """The first time at least `gap_hours` after now and after the channel's latest post
    date (FR-PUB-4). Abbas picks the real time when he schedules the draft."""
    gap = timedelta(hours=gap_hours)
    slot = ctx.now() + gap
    latest = ctx.conn.execute(
        "SELECT MAX(scheduled_at) FROM posts WHERE channel_id = ? AND scheduled_at IS NOT NULL",
        (channel_id,),
    ).fetchone()[0]
    if latest:
        slot = max(slot, datetime.fromisoformat(latest.replace("Z", "+00:00")) + gap)
    return slot


def _settings(platform: str, copy: PostCopy, disclose: bool) -> dict:
    if platform == "youtube":
        return youtube_settings(copy.title, ai_disclosure=disclose)
    if platform == "tiktok":
        return tiktok_settings(ai_disclosure=disclose)
    raise ToolError(f"no {platform} route for avatar posts yet (ADR 0002)")


def create_draft(ctx: ToolContext, args: DraftInput) -> dict:
    providers = require_providers(ctx)
    row = ctx.conn.execute(
        "SELECT r.path, e.brief_id FROM renders r JOIN edit_plans e ON e.id = r.edit_plan_id "
        "WHERE r.id = ?",
        (args.render_id,),
    ).fetchone()
    if row is None:
        raise ToolError(f"no render {args.render_id}")
    brief = require_brief(ctx, row["brief_id"])
    identity = require_identity(ctx, brief["identity_id"])
    require_qa_passed(ctx, args.render_id)  # the stored verdicts, not only the post state
    copies = {c.platform: c for c in args.posts}
    texts = post_texts([], [], [c.model_dump() for c in args.posts])
    flagged = [r for r in text_checks(ctx, identity, texts) if r.result != "pass"]
    if flagged:  # FR-QA-8 covers titles, captions and hashtags too
        reasons = "; ".join(reason for r in flagged for reason in r.reasons)
        raise ToolError(f"the post copy does not pass QA: {reasons}")
    posts = ctx.conn.execute(
        "SELECT p.*, c.platform FROM posts p JOIN channels c ON c.id = p.channel_id "
        "WHERE p.brief_id = ? ORDER BY p.id",
        (brief["id"],),
    ).fetchall()
    done, todo = [], []
    for post in posts:  # check every channel before drafting any (no half-drafted posts)
        if post["state"] in DRAFTED and post["postiz_id"]:
            done.append({"post_id": post["id"], "platform": post["platform"],
                         "postiz_id": post["postiz_id"], "new": False})  # fmt: skip
            continue
        if post["state"] != "qa_passed" or post["render_id"] != args.render_id:
            raise ToolError(
                f"post {post['id']} ({post['platform']}) is {post['state']}; only qa_passed "
                f"posts of render {args.render_id} become drafts"
            )
        channel = next((c for c in identity.channels if c.platform == post["platform"]), None)
        if channel is None:
            raise ToolError(f"{identity.id} no longer has a {post['platform']} channel")
        if not channel.postiz_integration_id:
            raise ToolError(
                f"{identity.id} has no Postiz integration for {channel.platform} (G0.7)"
            )
        copy = copies.get(post["platform"])
        if copy is None:
            raise ToolError(f"no title and caption given for {post['platform']}")
        settings = _settings(post["platform"], copy, bool(post["ai_disclosure"]))
        todo.append((post, channel.postiz_integration_id, copy, settings))
    for post, integration, copy, settings in todo:
        content = copy.caption + ("\n\n" + " ".join(copy.hashtags) if copy.hashtags else "")
        publish_at = _next_slot(ctx, post["channel_id"], identity.cadence.min_gap_hours)
        try:
            postiz_id = draft_post(
                providers.publisher, integration, post["platform"], content,
                [Path(row["path"])], settings, publish_at,
            )  # fmt: skip
        except PostizError as exc:
            raise ToolError(str(exc)) from exc
        ctx.conn.execute(
            "UPDATE posts SET state = 'drafted', postiz_id = ?, title = ?, caption = ?, "
            "hashtags = ?, scheduled_at = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ?",
            (postiz_id, copy.title, copy.caption, json.dumps(copy.hashtags), _iso(publish_at),
             post["id"]),
        )  # fmt: skip
        done.append({"post_id": post["id"], "platform": post["platform"],
                     "postiz_id": postiz_id, "new": True})  # fmt: skip
    return {"drafts": done}
