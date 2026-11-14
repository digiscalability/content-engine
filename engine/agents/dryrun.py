"""`ce daily --dry-run` (SRD §5.2, NFR-8): one post per due identity, brief to MP4, with
every vendor faked and Claude's part played by a scripted director.

The run lives in its own folder, `media/dry-run/<run_id>/` (DB, fixture library, voice,
avatar inbox, renders), so it never touches the real queue, ledger or library. The
director calls the same tools as the agents, through the same guards (`GuardedTools`), in
the orchestrator prompt's order. ffmpeg and HyperFrames run for real: they are local tools,
and the MP4 is the point. The run goes on through the QA checks and a scripted review to
a draft on the fake Postiz. Then fake Abbas publishes it, and the real reconcile and metrics
jobs take it to `published` with a 24 h snapshot. ADRs 0012, 0013 and 0021.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from engine.agents.actions import resolve_look
from engine.agents.context import Providers, ToolContext
from engine.agents.hooks import GuardedTools
from engine.agents.schemas import BudgetUsed, FailedPost, IdentityResult, NeedsAbbas, RunReport
from engine.agents.state import start_run, sync_identities
from engine.config import Config
from engine.db import open_db
from engine.learn.metrics import pull_metrics
from engine.learn.reconcile import Change, parse, reconcile
from engine.library.assets import file_sha256
from engine.library.embeddings import FakeEmbedder
from engine.library.transcribe import ScriptAlignedTranscriber
from engine.produce.avatar import FakeAvatar
from engine.produce.editplan import LookRef
from engine.produce.ffmpeg import FfmpegError
from engine.produce.overlay import HyperFramesRenderer, OverlayRenderer
from engine.produce.process import Runner, output_tail, run_command
from engine.produce.render_ffmpeg import RenderSettings
from engine.produce.voice import FakeVoice
from engine.publish.postiz import FakePostiz
from engine.publish.youtube import WATCH, FakeVisibility
from engine.settings import REPO_ROOT
from engine.style.look import active_look_ref

FAKE_INTEGRATIONS = {"youtube": "yt-1", "tiktok": "tt-1"}  # FakePostiz's channels
FIXTURE_SOURCE = "dry-run-fixtures"
CLIP_SECONDS = 4
# Cutaway stand-ins drawn by ffmpeg (NFR-9: never real footage), landscape so the cover fit
# crops them as it will crop real B-roll.
FIXTURE_CLIPS = {
    "mandelbrot": f"mandelbrot=s=1920x1080:r=30,trim=duration={CLIP_SECONDS}",
    "life": (
        "life=s=480x270:r=30:mold=10:ratio=0.1:death_color=#1e293b:life_color=#f59e0b,"
        f"scale=1920:1080:flags=neighbor,trim=duration={CLIP_SECONDS}"
    ),
    "testsrc": f"testsrc2=s=1920x1080:r=30:d={CLIP_SECONDS}",
}
TALK_SECONDS = 6.0  # format C: cut away from the presenter every 5 to 8 s
CUT_SECONDS = 2.0
EPS = 1e-6

# The post the director writes. It follows the selfhelp playbook's rules: a principle, no
# anecdote, nothing about Abbas, no health, money or legal claims (FR-QA-6, FR-QA-8).
BEATS = [
    {"text": "Most plans fail before they start, because the first step is too big.",
     "on_screen": "The first step is too big", "target_s": 5},
    {"text": "So shrink it until starting feels almost silly.", "target_s": 5},
    {"text": "Not write the chapter. Open the file and write one line.", "target_s": 5},
    {"text": "Not clean the kitchen. Wash one cup.", "target_s": 5},
    {"text": "A small start is not the goal. It is the doorway.", "target_s": 5},
    {"text": "Once you are moving, keeping going costs far less than beginning.",
     "target_s": 5},
    {"text": "Tomorrow, pick the task you keep avoiding and cut it in half twice.",
     "target_s": 5},
    {"text": "Then do the tiny version today. Follow for more on discipline and craft.",
     "target_s": 5},
]  # fmt: skip


def brief_for(identity_id: str, plan: dict | None = None) -> dict:
    """The sample brief; with a `get_topic_candidates` result it takes the first topic, the
    format and the exploration flag from it, as the scriptwriter prompt asks."""
    plan = plan or {}
    offered = plan.get("candidates") or [{"topic": "starting small"}]
    return {
        "identity_id": identity_id,
        "format": plan.get("format", "C"),
        "topic": offered[0]["topic"],
        "angle": "craft: shrink the first step until it is easy to take",
        "hook": "Most plans fail before they start",
        "cta": "follow",
        "difference_note": "Dry-run sample post written by the engine's director, not by Claude",
        "exploration": bool(plan.get("exploration", False)),
    }


def dry_run_config(config: Config) -> Config:
    """The config with fake vendor ids, so the action tools run without Gate 0b."""
    identities = {}
    for key, identity in config.identities.items():
        channels = [
            c.model_copy(update={"postiz_integration_id": FAKE_INTEGRATIONS.get(c.platform, "")})
            for c in identity.channels
        ]
        identities[key] = identity.model_copy(
            update={
                "voice_id": f"dry-run-voice-{identity.id}",
                "avatar_id": f"dry-run-avatar-{identity.id}",
                "channels": channels,
            }
        )
    return replace(config, identities=identities)


def fixture_library(ctx: ToolContext, run: Runner) -> list[int]:
    """Generate the cutaway clips and register them as own footage of a dry-run source."""
    folder = ctx.media_root / "library"
    folder.mkdir(parents=True, exist_ok=True)
    ctx.conn.execute(
        "INSERT OR IGNORE INTO sources (id, kind, path, rights_type, rights_proof) "
        "VALUES (?, 'folder', ?, 'own', ?)",
        (FIXTURE_SOURCE, str(folder), "Drawn by ffmpeg for the dry run; no real footage (NFR-9)"),
    )
    ids = []
    for name, graph in FIXTURE_CLIPS.items():
        path = folder / f"{name}.mp4"
        argv = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", graph,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                str(path)]  # fmt: skip
        result = run(argv, None)
        if result.returncode != 0:
            raise FfmpegError(f"fixture clip {name} failed: {output_tail(result)}")
        ids.append(
            ctx.conn.execute(
                "INSERT INTO assets (source_id, sha256, path, media_kind, duration, width, "
                "height, fps, rights_type, people_release) "
                "VALUES (?, ?, ?, 'video', ?, 1920, 1080, 30, 'own', 'none')",
                (FIXTURE_SOURCE, file_sha256(path), str(path), CLIP_SECONDS),
            ).lastrowid
        )
    return ids


def start_dry_run(
    config: Config,
    root: Path,
    run_id: str,
    today: date | None = None,
    run: Runner = run_command,
    overlay: OverlayRenderer | None = None,
) -> ToolContext:
    """A fresh DB, fixture library and fake providers under `root/run_id`."""
    folder = root / run_id
    config = dry_run_config(config)
    conn = open_db(folder / "engine.db")
    sync_identities(conn, config)
    start_run(conn, run_id, "ce daily --dry-run")
    voice = FakeVoice()
    providers = Providers(
        voice=voice,
        avatar=FakeAvatar(inbox=folder / "inbox" / "avatar", run=run),
        publisher=FakePostiz(),
        transcriber=ScriptAlignedTranscriber,
        overlay=overlay or HyperFramesRenderer(run=run),
        embedder=FakeEmbedder(),
        render_settings=RenderSettings.from_engine(config.engine),
        run=run,
    )
    ctx = ToolContext(
        conn=conn,
        config=config,
        run_id=run_id,
        today=today or date.today(),
        voice_balance=voice.balance(),
        providers=providers,
        media_root=folder,
        gpu_lock=config.engine.paths.resolve("gpu_lock", REPO_ROOT),
    )
    fixture_library(ctx, run)
    return ctx


def director_plan(
    voice: int,
    avatar: int,
    duration: float,
    cutaways: Sequence[tuple[int, float]],
    look: str,
    look_version: int = 1,
    talk_seconds: float = TALK_SECONDS,
) -> dict:
    """Format C: the presenter talks, a cutaway every ~6 s, the presenter closes. Avatar
    shots take source time equal to timeline time, so the lips stay on the voice."""
    shots: list[dict] = []
    t, n = 0.0, 0
    while t < duration - EPS:
        end = min(t + talk_seconds, duration)
        if duration - end <= CUT_SECONDS + 1.0:  # leave the ending to the presenter
            end = duration
        shots.append({"asset_id": avatar, "in": round(t, 3), "out": round(end, 3),
                      "role": "avatar"})  # fmt: skip
        t = end
        if t >= duration - EPS or not cutaways:
            break
        clip, clip_seconds = cutaways[n % len(cutaways)]
        n += 1
        length = min(CUT_SECONDS, clip_seconds)
        shots.append({"asset_id": clip, "in": 0, "out": length, "role": "broll"})
        t += length
    return {
        "timeline": shots,
        "audio": {"voice": [{"asset_id": voice, "at": 0, "in": 0, "out": round(duration, 3)}]},
        "captions": {"block": "captions"},
        "look": {"id": look, "version": look_version},
    }


@dataclass
class Produced:
    identity: str
    brief_id: int | None = None
    script_id: int | None = None
    edit_plan_id: int | None = None
    render_id: int | None = None
    mp4: str | None = None
    report: str | None = None
    exports: dict[str, str] = field(default_factory=dict)
    drafts: list[str] = field(default_factory=list)  # Postiz post ids
    failed: list[FailedPost] = field(default_factory=list)
    holds: list[str] = field(default_factory=list)
    needs: list[NeedsAbbas] = field(default_factory=list)


@dataclass
class FollowUp:
    states: dict[int, str] = field(default_factory=dict)  # post id -> state at the end
    changes: list[Change] = field(default_factory=list)
    snapshots: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class DryRunResult:
    run_id: str
    root: Path
    report: RunReport
    produced: list[Produced] = field(default_factory=list)
    follow_up: FollowUp | None = None


DRY_COUNTS = {"Views": 120, "Likes": 9, "Comments": 2, "Favorites": 0}  # YouTube's labels


def follow_up(ctx: ToolContext, now: datetime) -> FollowUp:
    """Play Abbas in the fake Postiz and YouTube Studio: schedule each draft, publish it and
    make it public. Then the real reconcile and metrics jobs run on the run's DB, with the
    metrics clock 25 h after publish (§10, FR-LRN-1; ADR 0021)."""
    postiz = ctx.providers.publisher
    rows = ctx.conn.execute(
        "SELECT p.id, p.postiz_id, c.platform FROM posts p JOIN channels c ON c.id = "
        "p.channel_id WHERE p.state = 'drafted' AND p.postiz_id IS NOT NULL ORDER BY p.id"
    ).fetchall()
    public = set()
    for post in rows:
        video = f"dryrun{post['id']:05d}"  # shaped like a YouTube video id
        postiz.schedule(post["postiz_id"])
        postiz.publish(post["postiz_id"], WATCH + video, video)
        postiz.set_metrics(post["postiz_id"], **DRY_COUNTS)
        public.add(video)
    done = FollowUp()
    if not rows:
        return done
    done.changes = reconcile(ctx.conn, postiz, FakeVisibility(public=public), now).changes
    (latest,) = ctx.conn.execute(
        "SELECT MAX(published_at) FROM posts WHERE state = 'published'"
    ).fetchone()
    if latest:
        later = parse(latest) + timedelta(hours=25)
        done.snapshots = pull_metrics(ctx.conn, postiz, later).taken
    ids = [post["id"] for post in rows]
    done.states = {
        r["id"]: r["state"]
        for r in ctx.conn.execute(
            f"SELECT id, state FROM posts WHERE id IN ({', '.join('?' * len(ids))})", ids
        )
    }
    return done


def copy_for(platform: str) -> dict:
    """The director's title, caption and hashtags for the sample post."""
    return {
        "platform": platform,
        "title": "Make the first step embarrassingly small",
        "caption": "Shrink the task until starting feels almost silly, then keep going.",
        "hashtags": ["#discipline", "#habits", "#craft"],
    }


def _direct_one(
    tools: GuardedTools, identity: dict, made: Produced, warnings: list[str], look_ref: LookRef
) -> None:
    """One post, in the orchestrator prompt's order; stops at the first refusal."""
    name = identity["id"]

    def failed(step: str, result: dict) -> None:
        reason = "; ".join(result.get("errors", [])) or "unknown error"
        made.failed.append(FailedPost(post=f"{name} {step}", reason=reason))

    look, _ = resolve_look(tools.ctx, look_ref)  # its pacing sets the cut rhythm
    tools.call("get_playbook", {"identity": name})
    plan = tools.call("get_topic_candidates", {"identity": name})
    tools.call("get_style_profile", {"identity": name})
    library = tools.call("search_library", {"format": "C", "limit": 10}).get("results", [])
    cutaways = list({r["asset_id"]: (r["asset_id"], r["duration"]) for r in library}.values())
    if not cutaways:
        warnings.append(f"{name}: no cutaway footage in the library; the presenter only")

    brief = tools.call("submit_brief", brief_for(name, plan if plan["ok"] else None))
    if not brief["ok"]:
        return failed("brief", brief)
    made.brief_id = brief["brief_id"]
    script = tools.call("submit_script", {"brief_id": brief["brief_id"], "beats": BEATS})
    if not script["ok"]:
        return failed("script", script)
    made.script_id = script["script_id"]

    tools.call("budget_status", {})
    voice = tools.call("synthesize_voice", {"script_id": script["script_id"]})
    if not voice["ok"]:
        return failed("voice", voice)
    avatar = tools.call(
        "request_avatar", {"brief_id": brief["brief_id"], "script_id": script["script_id"]}
    )
    if not avatar["ok"]:
        return failed("avatar", avatar)
    if avatar["state"] == "waiting":
        made.needs.append(NeedsAbbas(item=avatar["detail"]))
        return None

    plan = director_plan(
        voice["asset_id"],
        avatar["asset_id"],
        voice["duration"],
        cutaways,
        look=look_ref.id,
        look_version=look_ref.version,
        talk_seconds=sum(look.pacing.cut_every_s) / 2,
    )
    edit = tools.call("submit_edit_plan", {"brief_id": brief["brief_id"], "plan": plan})
    if not edit["ok"]:
        return failed("edit plan", edit)
    made.edit_plan_id = edit["edit_plan_id"]
    rendered = tools.call("render_post", {"edit_plan_id": edit["edit_plan_id"]})
    if not rendered["ok"]:
        return failed("render", rendered)
    render_id = rendered["render_id"]
    made.render_id, made.mp4 = render_id, rendered["path"]
    made.report, made.exports = rendered["report"], rendered["exports"]

    checks = tools.call("run_qa_checks", {"render_id": render_id})
    if not checks["ok"]:
        return failed("QA checks", checks)
    if not checks["passed"]:
        for check in checks["checks"]:
            reasons = "; ".join(check["reasons"])
            if check["result"] == "hold":
                made.holds.append(f"render {render_id} {check['name']}: {reasons}")
            elif check["result"] == "fail":
                made.failed.append(
                    FailedPost(
                        post=f"{name} render {render_id}", reason=f"{check['name']}: {reasons}"
                    )
                )
        return None
    tools.call("get_render_report", {"render_id": render_id})  # what the reviewer reads
    verdict = {
        "render_id": render_id,
        "verdict": "pass",
        "adds": ["script", "education"],
        "fits_identity": True,
    }  # the director's own script and framing (GR-3)
    review = tools.call("submit_qa_review", verdict)
    if not review["ok"]:
        return failed("QA review", review)
    copy = [copy_for(platform) for platform in identity["channels"]]
    drafted = tools.call("create_draft", {"render_id": render_id, "posts": copy})
    if not drafted["ok"]:
        return failed("draft", drafted)
    made.drafts = [d["postiz_id"] for d in drafted["drafts"]]
    return None


def direct(
    ctx: ToolContext, only: str | None = None, look_version: int | None = None
) -> tuple[RunReport, list[Produced]]:
    """Play the orchestrator, scriptwriter and qa-reviewer for every due identity."""
    tools = GuardedTools(ctx)
    warnings = [
        "dry run: every vendor is faked and Claude's part is scripted (NFR-8)",
        "dry run: the qa-reviewer's verdict is scripted (pass); the deterministic checks ran",
    ]
    results, needs, produced = [], [], []
    for identity in tools.call("get_state", {})["identities"]:
        if only and identity["id"] != only:
            continue
        if not identity["due"]:
            reason = "kill flag set" if identity["kill_flag"] else "not due this week"
            warnings.append(f"{identity['id']}: skipped ({reason})")
            continue
        made = Produced(identity=identity["id"])
        config = ctx.config.identities[identity["id"]]
        ref = (
            LookRef(id=identity["id"], version=look_version)
            if look_version
            else active_look_ref(config)
        )
        _direct_one(tools, identity, made, warnings, ref)
        produced.append(made)
        needs += made.needs
        results.append(
            IdentityResult(
                identity=made.identity,
                posts_drafted=len(made.drafts),
                posts_failed=made.failed,
                qa_holds=made.holds,
            )
        )
    chars = ctx.conn.execute(
        "SELECT COALESCE(SUM(units), 0) FROM budget_ledger WHERE vendor = 'elevenlabs'"
    ).fetchone()[0]
    report = RunReport(
        run_id=ctx.run_id,
        identity_results=results,
        budget_used=BudgetUsed(elevenlabs_chars=int(chars)),
        warnings=warnings,
        needs_abbas=needs,
    )
    return report, produced


def new_run_id(now: datetime | None = None) -> str:
    return f"dry-{(now or datetime.now()):%Y%m%d-%H%M%S}"


def daily_dry_run(
    config: Config,
    identity: str | None = None,
    root: Path | None = None,
    run_id: str | None = None,
    today: date | None = None,
    run: Runner = run_command,
    overlay: OverlayRenderer | None = None,
) -> DryRunResult:
    run_id = run_id or new_run_id()
    root = root or config.engine.paths.resolve("media", REPO_ROOT) / "dry-run"
    ctx = start_dry_run(config, root, run_id, today=today, run=run, overlay=overlay)
    report, produced = direct(ctx, identity)
    followed = follow_up(ctx, datetime.now(UTC))
    if followed.changes:
        report.warnings.append(
            "dry run: fake Abbas scheduled, published and made public each draft; "
            "reconcile and metrics then ran for real on this run's DB"
        )
    text = report.model_dump_json(indent=1)
    (ctx.media_root / "run_report.json").write_text(text, encoding="utf-8")
    ctx.conn.execute(
        "UPDATE agent_runs SET report = ?, subtype = 'dry_run', "
        "ended_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE run_id = ?",
        (json.dumps(json.loads(text)), run_id),
    )
    return DryRunResult(run_id=run_id, root=ctx.media_root, report=report, produced=produced,
                        follow_up=followed)  # fmt: skip
