"""The walkthrough video (SRD P1.8, §1.2: a 2-3 minute demo produced by the engine itself;
ADR 0024), rendered from one real `ce daily --dry-run`. Local only.

    uv run python scripts/make_demo.py [<dry-run folder>] [--out docs/media]

It reads the run's own database (brief, script, edit plan, assets, QA results, post, metrics
snapshot, and the audit log `tool_calls`), its render report and the Short it made, and lays
them out as ten timed chapters in one HyperFrames composition, rendered with the engine's
pinned HyperFrames. The replay chapter (from P0.7b, ADR 0014) replays the tool calls with
their real durations, sped up only while the video renders, and says so. Every number on
screen comes from the run; model-written text is escaped before it enters the HTML.

Writes `walkthrough.mp4` (1280x720, silent: the dry run's voice is a silent fake) and
`demo.gif`, the replay chapter alone, for the README.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from string import Template

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from engine.produce.overlay import HYPERFRAMES_ENV, HYPERFRAMES_VERSION  # noqa: E402
from engine.produce.process import run_command  # noqa: E402

FPS = 30
TYPE_START, CHAR_STEP = 0.6, 0.045
LINES_START, LINE_STEP = 2.1, 0.32
RENDER_SHOWN = 5.0  # seconds of playback for the long render
SUMMARY_STEP = 0.25
REPLAY_SHORT = 8.0  # seconds the replay shows the Short on its phone
COMMAND = "uv run ce daily --dry-run"
ROW_START, CARD_HOLD, CARD_MIN = 2.2, 3.0, 10.0  # a card's rows start, its end hold, its floor
FADE = 0.35  # a card fades out over its last FADE seconds
READ_BASE, READ_PER_WORD = 0.5, 0.3  # a line stays at least this long once it appears
SHORT_MAX, SHORT_LEAD, SHORT_PAD = 40.0, 0.3, 0.8  # at most 40 s of the Short, from 0.3 s
CHECK_LABELS = {
    "rights": "Rights",
    "disclosure": "AI disclosure",
    "banned_topics": "Banned topics",
    "claims_about_abbas": "Claims about the presenter",
    "self_dedupe": "Self-dedupe",
    "spec": "Platform spec",
    "qa_review": "Reviewer (scripted in a dry run)",
}
RESULT_CLASS = {"pass": "ok", "hold": "warn", "fail": "bad"}
Row = tuple[str, str] | tuple[str, str, str]  # label, value, optional class ok/warn/bad


def seconds_label(ms: int) -> str:
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def tool_rows(db: Path) -> list[tuple[str, str, int]]:
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT tool, status, duration_ms FROM tool_calls ORDER BY id").fetchall()
    conn.close()
    return [(tool.removeprefix("mcp__engine__"), status, ms or 0) for tool, status, ms in rows]


def tool_timeline(rows: list[tuple[str, str, int]]) -> tuple[list[dict], float]:
    """When each tool line appears; the render gets a progress bar of RENDER_SHOWN seconds."""
    events, t = [], LINES_START
    for tool, status, ms in rows:
        long = ms >= 10_000
        events.append(
            {
                "tool": tool,
                "status": status,
                "took": seconds_label(ms),
                "at": t,
                "bar": RENDER_SHOWN if long else 0,
                "speedup": ms / 1000 / RENDER_SHOWN,
            }
        )
        t += (RENDER_SHOWN if long else 0) + LINE_STEP
    return events, t


def summary_lines(run: Path) -> list[str]:
    report = json.loads((run / "run_report.json").read_text(encoding="utf-8"))
    rel = Path("media") / "dry-run" / run.name
    drafted = sum(r["posts_drafted"] for r in report["identity_results"])
    lines = [f"Run {run.name} wrote ({drafted} fake Postiz draft):"]
    for folder in sorted((run / "renders").iterdir()):
        name = folder.name
        for file in (f"{name}.mp4", "render_report.json", f"{name}.otio", f"{name}.fcpxml"):
            lines.append(f"  {rel / 'renders' / name / file}")
    lines.append(f"  {rel / 'run_report.json'}")
    return lines


@dataclass(frozen=True)
class RunFacts:
    """What the walkthrough shows, read from one dry run (the first render if it made more)."""

    run: Path
    run_id: str
    brief: dict
    beats: list[tuple[float, str]]  # start second, spoken text
    shots: list[tuple[str, str, float]]  # role, rights tier, seconds
    plan: dict
    report: dict
    checks: list[tuple[str, str]]  # check name, result
    post: dict | None  # state, title, hashtags
    views_24h: int | None
    likes_24h: int | None
    tool_rows: list[tuple[str, str, int]]
    tool_calls: int
    tool_calls_ok: int
    summary: list[str]
    short: Path
    short_seconds: float


def run_facts(run: Path) -> RunFacts:
    """The run's first render (by id) and what belongs to it, followed by key: render, then
    its edit plan, brief and newest script, its QA rows, its first post and that post's 24 h
    snapshot. A run with more identities shows the first one only."""
    conn = sqlite3.connect(run / "engine.db")
    conn.row_factory = sqlite3.Row
    one = conn.execute
    render = one("SELECT id, edit_plan_id, path FROM renders ORDER BY id").fetchone()
    if render is None:
        raise SystemExit(f"{run.name} has no render: the walkthrough needs a dry run that "
                         "got as far as the render.")  # fmt: skip
    plan_row = one("SELECT brief_id, plan FROM edit_plans WHERE id = ?",
                   (render["edit_plan_id"],)).fetchone()  # fmt: skip
    plan = json.loads(plan_row["plan"])
    brief = dict(
        one(
            "SELECT topic, angle, hook, cta, difference_note, exploration FROM briefs WHERE id = ?",
            (plan_row["brief_id"],),
        ).fetchone()
    )
    script = one(
        "SELECT body FROM scripts WHERE brief_id = ? ORDER BY version DESC, id DESC",
        (plan_row["brief_id"],),
    ).fetchone()
    beats, t = [], 0.0
    for beat in json.loads(script["body"])["beats"]:
        beats.append((t, beat["text"]))
        t += float(beat["target_s"])
    tiers = {r["id"]: r["rights_type"] for r in one("SELECT id, rights_type FROM assets")}
    shots = [
        (s["role"], tiers.get(s["asset_id"], "unknown"), round(s["out"] - s["in"], 2))
        for s in plan["timeline"]
    ]
    checks = [
        (r["check_name"], r["result"])
        for r in one(
            "SELECT check_name, result FROM qa_results WHERE render_id = ? ORDER BY id",
            (render["id"],),
        )
    ]
    post_row = one(
        "SELECT id, state, title, hashtags FROM posts WHERE render_id = ? ORDER BY id",
        (render["id"],),
    ).fetchone()
    post = None
    if post_row is not None:
        post = {
            "state": post_row["state"],
            "title": post_row["title"],
            "hashtags": json.loads(post_row["hashtags"] or "[]"),
        }
    snap = None
    if post_row is not None:
        snap = one(
            "SELECT views, likes FROM metrics_snapshots WHERE post_id = ? AND at = '24h'",
            (post_row["id"],),
        ).fetchone()
    conn.close()
    rows = tool_rows(run / "engine.db")
    recorded = Path(render["path"])
    short = run / "renders" / recorded.parent.name / recorded.name  # the run folder may move
    report = json.loads((short.parent / "render_report.json").read_text(encoding="utf-8"))
    return RunFacts(
        run=run,
        run_id=run.name,
        brief=brief,
        beats=beats,
        shots=shots,
        plan=plan,
        report=report,
        checks=checks,
        post=post,
        views_24h=None if snap is None else snap["views"],
        likes_24h=None if snap is None else snap["likes"],
        tool_rows=rows,
        tool_calls=len(rows),
        tool_calls_ok=sum(1 for _, status, _ in rows if status == "ok"),
        summary=summary_lines(run),
        short=short,
        short_seconds=float(report["duration"]),
    )


@dataclass(frozen=True)
class Chapter:
    name: str
    start: float
    duration: float


def esc(text: object) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def card_seconds(rows: list[Row], step: float) -> float:
    """Long enough to show every row, hold the last, and let the longest line be read."""
    read = max(
        (ROW_START + i * step + READ_BASE + READ_PER_WORD * len(str(row[1]).split()) + FADE
         for i, row in enumerate(rows)),
        default=0.0,
    )  # fmt: skip
    return round(max(CARD_MIN, ROW_START + len(rows) * step + CARD_HOLD, read), 2)


def replay_layout(facts: RunFacts) -> tuple[list[dict], float, float, float]:
    """Tool events, when the summary starts, when the Short appears, and the chapter length."""
    events, t = tool_timeline(facts.tool_rows)
    render_done = next((e["at"] + e["bar"] for e in events if e["bar"]), t)
    short_start = round(render_done + 0.4, 3)
    summary_at = t + 0.3
    summary_end = summary_at + len(facts.summary) * SUMMARY_STEP
    length = round(max(summary_end + 1.5, short_start + REPLAY_SHORT), 1)
    return events, summary_at, short_start, length


def _intro_rows(f: RunFacts) -> list[Row]:
    return [
        ("", f"Dry run {f.run_id}: one self-help Short from topic to a Postiz draft, then a "
             "fake owner publishes it and the engine follows the post."),
        ("", "Every vendor is faked: a silent voice, a placeholder avatar, B-roll drawn by "
             "ffmpeg. Claude's part is scripted, so no key or account is used."),
        ("", "Every number on screen is read from that run's database and files, and this "
             "video was rendered from them with the engine's pinned HyperFrames."),
    ]  # fmt: skip


def _brief_rows(f: RunFacts) -> list[Row]:
    b = f.brief
    return [
        ("Topic", b["topic"]),
        ("Angle", b["angle"]),
        ("Hook", b["hook"]),
        ("Call to action", b["cta"] or "none"),
        ("Why it is new", b["difference_note"] or "not stated"),
        ("Exploration", "yes" if b["exploration"] else "no: an established pillar"),
    ]


def _script_rows(f: RunFacts) -> list[Row]:
    return [(f"{start:.0f} s", text) for start, text in f.beats]


def _plan_rows(f: RunFacts) -> list[Row]:
    rows: list[Row] = [
        (f"Shot {i}", f"{role}, {tier}, {seconds:.1f} s")
        for i, (role, tier, seconds) in enumerate(f.shots, start=1)
    ]
    audio = f.plan.get("audio", {})
    look = f.plan.get("look") or {}
    rows += [
        ("Voice", f"{len(audio.get('voice') or [])} synthesized track"),
        ("Music", "a library track" if audio.get("music") else "none in this plan"),
        ("Captions", f"the identity's '{(f.plan.get('captions') or {}).get('block')}' block"),
        ("Look", f"{look.get('id')} v{look.get('version')}"),
    ]
    return rows


def _short_rows(f: RunFacts) -> list[Row]:
    r = f.report
    video, loud = r["video"], r["loudness"]
    if loud.get("integrated_lufs") is None:
        loudness = f"silent mix: the fake voice is silent (target {loud['target_lufs']:g} LUFS)"
    else:
        loudness = (
            f"{loud['integrated_lufs']:.1f} LUFS, true peak {loud['true_peak_dbtp']:.1f} dBTP"
        )
    tiers = ", ".join(f"{tier} {seconds:.1f} s" for tier, seconds in r["tiers"].items())
    people = r.get("people", {})
    faces = (
        "no person in any shot"
        if set(people) <= {"none"}
        else "; ".join(f"{k}: {len(v)} assets" for k, v in people.items())
    )
    caps = r.get("captions", {})
    return [
        ("Video", f"{video['width']}×{video['height']}, {video['fps']:g} fps, "
                  f"{video['codec'].upper()}"),
        ("Length", f"{r['duration']:.1f} s"),
        ("Loudness", loudness),
        ("Rights", f"{tiers}; film {r['film']['seconds']:.1f} s"),
        ("People", faces),
        ("Captions", f"{caps.get('words')} words in {caps.get('groups')} groups, "
                     f"{caps.get('coverage', 0):.0%} covered"),
        ("Exports", ".otio and .fcpxml beside the MP4"),
    ]  # fmt: skip


def _qa_rows(f: RunFacts) -> list[Row]:
    return [
        (CHECK_LABELS.get(name, name.replace("_", " ")), result, RESULT_CLASS.get(result, ""))
        for name, result in f.checks
    ]


def _after_rows(f: RunFacts) -> list[Row]:
    exports = f.report.get("exports", {})
    exported = (f"{exports.get('otio')} and {exports.get('fcpxml')} open the edit in "
                "DaVinci Resolve for hand polish")  # fmt: skip
    if f.post is None:
        return [("Draft", "none: the QA gate did not pass this render", "warn"),
                ("Exports", exported)]  # fmt: skip
    views = (
        "no snapshot yet"
        if f.views_24h is None
        else f"{f.views_24h} views, {f.likes_24h} likes (numbers from the fake Postiz)"
    )
    return [
        ("Draft", "a Postiz draft: at autonomy level 0 the engine never schedules"),
        ("Owner", "schedules or deletes it in Postiz; here a fake owner published it"),
        ("Reconcile", f"post state now: {f.post['state']}", "ok"),
        ("Metrics", f"24 h snapshot: {views}"),
        ("Title", f.post["title"]),
        ("Hashtags", " ".join(f.post["hashtags"])),
        ("Exports", exported),
    ]


def _guard_rows(f: RunFacts) -> list[Row]:
    return [
        ("Tools only", "the agent has no shell, no web and no file writes; vendor keys never "
                       "reach it"),
        ("H1 publish", "nothing is scheduled at autonomy level 0; holds wait for the owner"),
        ("H2 read", "only config, prompts and the work folder, checked on real paths"),
        ("H3 budget", "voice, avatar and render caps checked before money is spent"),
        ("H4 rights", "an edit plan must be rights-clean before it is stored"),
        ("H5 audit", f"every call is a row in tool_calls: this run made {f.tool_calls}, "
                     f"{f.tool_calls_ok} ok"),
    ]  # fmt: skip


def _outro_rows(f: RunFacts) -> list[Row]:
    return [
        ("uv run pytest", "unit and contract tests on fakes: no keys, GPU or network"),
        ("-m golden", "real ffmpeg and HyperFrames renders, checked by measurement"),
        ("-m model, -m gpu", "the real embedding models; Whisper large-v3 on the GPU"),
        ("CI", "ruff and the pure-module tests on every push, offline, on Windows"),
        ("Code", "github.com/digiscalability/content-engine"),
    ]


# name, kicker title, subtitle, rows, seconds between rows
CARDS = {
    "intro": ("content-engine", "One Short from topic to draft, made by the engine on fakes",
              _intro_rows, 1.2),
    "brief": ("The brief", "Picked from the identity's topic bank by get_topic_candidates; "
              "submit_brief refuses banned terms and first-person wording.", _brief_rows, 1.5),
    "script": ("The script", "Timed beats, written to be spoken: no claims about the presenter "
               "beyond the public fact sheet.", _script_rows, 1.5),
    "edit_plan": ("The edit plan", "A validated JSON shot list, not pixels. The rights check "
                  "runs before it is stored (guard H4).", _plan_rows, 0.8),
    "qa": ("The QA gate", "Deterministic checks first; the reviewer judges only a render that "
           "passed them all, and its verdict is final.", _qa_rows, 1.0),
    "after": ("After the draft", "The engine drafts; the owner decides. Reconcile and metrics "
              "then follow the post through Postiz.", _after_rows, 1.3),
    "guards": ("Guards", "The agent acts only through engine tools, and every call passes one "
               "guard table. A guard that crashes blocks the call.", _guard_rows, 1.4),
    "outro": ("How it is tested", "Each external service sits behind a protocol with a fake; a "
              "contract test runs the adapter and its fake through the same cases.", _outro_rows,
              1.0),
}  # fmt: skip
ORDER = ["intro", "replay", "brief", "script", "edit_plan", "short", "qa", "after", "guards",
         "outro"]  # fmt: skip


def chapters(facts: RunFacts) -> list[Chapter]:
    out, t = [], 0.0
    for name in ORDER:
        if name == "replay":
            length = replay_layout(facts)[3]
        elif name == "short":
            length = min(facts.short_seconds, SHORT_MAX) + SHORT_PAD
        else:
            _, _, rows, step = CARDS[name]
            length = card_seconds(rows(facts), step)
        out.append(Chapter(name, round(t, 2), round(length, 2)))
        t += length
    return out


def teaser_window(chs: list[Chapter]) -> tuple[float, float]:
    replay = next(c for c in chs if c.name == "replay")
    return replay.start, replay.start + replay.duration


CARD_STYLE = """
    #root { position: absolute; inset: 0; background: #0b1020; color: #e2e8f0;
            font-family: "Inter", sans-serif; }
    .kicker { position: absolute; left: 96px; top: 60px; font-size: 28px; letter-spacing: 3px;
              color: #f59e0b; text-transform: uppercase; opacity: 0; }
    .title { position: absolute; left: 96px; top: 100px; font-size: 60px; font-weight: 700;
             opacity: 0; }
    .sub { position: absolute; left: 96px; top: 186px; width: 1640px; font-size: 27px;
           line-height: 38px; color: #94a3b8; opacity: 0; }
    .rows { position: absolute; left: 96px; top: 300px; width: 1728px; }
    .row { display: grid; grid-template-columns: 330px 1fr; column-gap: 32px; font-size: 32px;
           line-height: 44px; margin-bottom: 20px; opacity: 0; }
    .row.wide { display: block; font-size: 34px; line-height: 48px; margin-bottom: 30px; }
    .label { color: #94a3b8; font-family: "JetBrains Mono", monospace; font-size: 28px; }
    .ok { color: #34d399; } .warn { color: #f59e0b; } .bad { color: #f87171; }
    .progress { position: absolute; left: 0; bottom: 0; width: 1920px; height: 6px;
                background: #f59e0b; transform-origin: left center; opacity: 0.85; }
"""

CARD = Template("""<template>
  <style>$style</style>
  <div id="root" data-composition-id="$name" data-width="1920" data-height="1080">
    <div id="$name-wrap" class="$variant">
      <div id="$name-kicker" class="kicker">$kicker</div>
      <div id="$name-title" class="title">$title</div>
      <div id="$name-sub" class="sub">$sub</div>
      <div class="rows">
$rows
      </div>
    </div>
    <div id="$name-progress" class="progress"></div>
  </div>
  <script>
    const tl = gsap.timeline({ paused: true });
    const show = (sel, at) => tl.fromTo(sel, { opacity: 0, y: 14 },
      { opacity: 1, y: 0, duration: 0.4, ease: "power2.out" }, at);
    tl.fromTo("#$name-progress", { scaleX: 0 }, { scaleX: 1, duration: $duration, ease: "none" },
      0);
    if ($first) {
      tl.fromTo(["#$name-kicker", "#$name-title"], { opacity: 1, y: 0 },
        { opacity: 1, y: 0, duration: 0.01 }, 0);
    } else {
      show("#$name-kicker", 0.15);
      show("#$name-title", 0.3);
    }
    show("#$name-sub", 0.8);
    $row_times.forEach((at, i) => show("#$name-r" + i, at));
    tl.to("#$name-wrap", { opacity: 0, duration: 0.3, ease: "power1.in" }, $fade_at);
    window.__timelines["$name"] = tl;
  </script>
</template>
""")


def card_html(
    name: str,
    title: str,
    rows: list[Row],
    sub: str = "",
    kicker: str = "",
    step: float = 1.0,
    duration: float | None = None,
    variant: str = "",
) -> str:
    """One chapter card. Every piece of text is escaped: briefs and scripts are model-written
    in live runs, and a stray `</template>` would end the scene."""
    duration = duration if duration is not None else card_seconds(rows, step)
    lines = []
    for i, row in enumerate(rows):
        label, value = row[0], row[1]
        cls = row[2] if len(row) > 2 else ""
        if label:
            lines.append(
                f'        <div id="{name}-r{i}" class="row"><span class="label">{esc(label)}'
                f'</span><span class="{cls}">{esc(value)}</span></div>'
            )
        else:
            lines.append(f'        <div id="{name}-r{i}" class="row wide">{esc(value)}</div>')
    times = [round(ROW_START + i * step, 3) for i in range(len(rows))]
    return CARD.substitute(
        style=CARD_STYLE, name=name, variant=variant, kicker=esc(kicker), title=esc(title),
        sub=esc(sub), rows="\n".join(lines), row_times=json.dumps(times),
        fade_at=round(duration - FADE, 3), duration=round(duration, 3),
        first="true" if variant == "first" else "false",
    )  # fmt: skip


REPLAY = Template("""<template>
  <style>
    #root { position: absolute; inset: 0; background: #0b1020; color: #e2e8f0;
            font-family: "Inter", sans-serif; }
    #replay-title { position: absolute; left: 60px; top: 40px; font-size: 44px;
                    font-weight: 700; }
    #replay-note { position: absolute; left: 60px; top: 100px; font-size: 28px;
                   color: #94a3b8; }
    #replay-term { position: absolute; left: 60px; top: 156px; width: 1250px; height: 896px;
                   background: #111827; border: 2px solid #1f2937; border-radius: 18px;
                   padding: 24px 28px; font-family: "JetBrains Mono", monospace; font-size: 24px;
                   line-height: 35px; }
    .rrow { white-space: pre; opacity: 0; }
    .rok { color: #34d399; }
    .rdim { color: #64748b; }
    .rbar { display: inline-block; width: 220px; height: 12px; margin: 0 14px 2px 0;
            vertical-align: middle; background: #1f2937; border-radius: 6px; overflow: hidden; }
    .rfill { display: block; width: 100%; height: 100%; background: #f59e0b;
             transform-origin: left center; }
    .rch { opacity: 0; }
    #replay-phone { position: absolute; left: 1380px; top: 156px; width: 468px; height: 832px;
                    border-radius: 34px; background: #000; border: 6px solid #1f2937;
                    overflow: hidden; opacity: 0; }
    #replay-short { width: 100%; height: 100%; object-fit: cover; }
    #replay-phone-note { position: absolute; left: 1350px; top: 1004px; width: 530px;
                         text-align: center; font-size: 22px; line-height: 28px; color: #94a3b8;
                         opacity: 0; }
  </style>
  <div id="root" data-composition-id="replay" data-width="1920" data-height="1080">
    <div id="replay-title">content-engine:
      <span style="color:#f59e0b">ce daily --dry-run</span></div>
    <div id="replay-note">Replay of a real run from its own audit log: the tool calls and timings
      are real; playback is sped up while the video renders.</div>
    <div id="replay-term">
      <div class="rrow"
        id="replay-prompt"><span class="rdim">PS content-engine&gt; </span>$typed</div>
$rows
    </div>
    <div id="replay-phone">
      <video id="replay-short" class="clip" src="assets/short.mp4" data-start="$short_start"
        data-duration="$short_duration" data-track-index="2" muted playsinline></video>
    </div>
    <div id="replay-phone-note">The Short it made, with every vendor faked: placeholder avatar,
      ffmpeg-drawn B-roll, captions.</div>
  </div>
  <script>
    const events = $events;
    const tl = gsap.timeline({ paused: true });
    tl.set("#replay-prompt", { opacity: 1 }, 0.2);
    document.querySelectorAll("#replay-prompt .rch").forEach((el, i) => {
      tl.set(el, { opacity: 1 }, $type_start + i * $char_step);
    });
    for (const e of events) {
      tl.set(e.row, { opacity: 1 }, e.at);
      if (e.fill) {
        tl.fromTo(e.fill, { scaleX: 0 }, { scaleX: 1, duration: e.bar, ease: "none" }, e.at);
        tl.set(e.done, { opacity: 1 }, e.at + e.bar);
      }
    }
    tl.to("#replay-phone", { opacity: 1, duration: 0.4, ease: "power2.out" }, $short_start - 0.3);
    tl.to("#replay-phone-note", { opacity: 1, duration: 0.4 }, $short_start);
    window.__timelines["replay"] = tl;
  </script>
</template>
""")


def replay_html(facts: RunFacts) -> str:
    events, summary_at, short_start, length = replay_layout(facts)
    html_rows, js_events = [], []
    for i, e in enumerate(events):
        row_id = f"replay-t{i}"
        mark = '<span class="rok">ok</span>' if e["status"] == "ok" else esc(e["status"])
        if e["bar"]:
            body = (
                f'{esc(e["tool"]):<21}<span class="rbar"><span class="rfill" id="replay-f{i}">'
                f'</span></span><span id="replay-d{i}" style="opacity:0">{mark} {e["took"]}'
                f'<span class="rdim">  (shown {e["speedup"]:.0f}x faster)</span></span>'
            )
            js_events.append({"row": f"#{row_id}", "at": round(e["at"], 3), "bar": e["bar"],
                              "fill": f"#replay-f{i}", "done": f"#replay-d{i}"})  # fmt: skip
        else:
            body = f'{esc(e["tool"]):<21}{mark} <span class="rdim">{e["took"]:>8}</span>'
            js_events.append({"row": f"#{row_id}", "at": round(e["at"], 3)})
        html_rows.append(
            f'      <div class="rrow" id="{row_id}">  <span class="rdim">&gt;</span> {body}</div>'
        )
    for j, line in enumerate(facts.summary):
        html_rows.append(f'      <div class="rrow" id="replay-s{j}">{esc(line)}</div>')
        js_events.append({"row": f"#replay-s{j}", "at": round(summary_at + j * SUMMARY_STEP, 3)})
    typed = "".join(f'<span class="rch">{esc(c)}</span>' for c in COMMAND)
    return REPLAY.substitute(
        rows="\n".join(html_rows), events=json.dumps(js_events), typed=typed,
        type_start=TYPE_START, char_step=CHAR_STEP, short_start=short_start,
        short_duration=round(length - short_start, 3),
    )  # fmt: skip


SHORT = Template("""<template>
  <style>$style
    #short-phone { position: absolute; left: 150px; top: 60px; width: 540px; height: 960px;
                   border-radius: 38px; background: #000; border: 6px solid #1f2937;
                   overflow: hidden; }
    #short-video { width: 100%; height: 100%; object-fit: cover; }
    #short-wrap .kicker, #short-wrap .title, #short-wrap .sub, #short-wrap .rows { left: 790px; }
    #short-wrap .sub { width: 1030px; }
    #short-wrap .rows { top: 330px; width: 1040px; }
    #short-wrap .row { grid-template-columns: 190px 1fr; font-size: 28px; line-height: 38px; }
  </style>
  <div id="root" data-composition-id="short" data-width="1920" data-height="1080">
    <div id="short-wrap">
      <div id="short-phone">
        <video id="short-video" class="clip" src="assets/short.mp4" data-start="$lead"
          data-duration="$clip" data-track-index="2" muted playsinline></video>
      </div>
      <div id="short-kicker" class="kicker">$kicker</div>
      <div id="short-title" class="title">The render</div>
      <div id="short-sub" class="sub">The Short in full, as ffmpeg and HyperFrames made it, beside
        what its render_report.json says.</div>
      <div class="rows">
$rows
      </div>
    </div>
    <div id="short-progress" class="progress"></div>
  </div>
  <script>
    const tl = gsap.timeline({ paused: true });
    const show = (sel, at) => tl.fromTo(sel, { opacity: 0, y: 14 },
      { opacity: 1, y: 0, duration: 0.4, ease: "power2.out" }, at);
    tl.fromTo("#short-progress", { scaleX: 0 }, { scaleX: 1, duration: $duration, ease: "none" },
      0);
    show("#short-kicker", 0.15);
    show("#short-title", 0.3);
    show("#short-sub", 0.8);
    $row_times.forEach((at, i) => show("#short-r" + i, at));
    tl.to("#short-wrap", { opacity: 0, duration: 0.3, ease: "power1.in" }, $fade_at);
    window.__timelines["short"] = tl;
  </script>
</template>
""")


def short_html(facts: RunFacts, kicker: str) -> str:
    rows = _short_rows(facts)
    lines = [
        f'        <div id="short-r{i}" class="row"><span class="label">{esc(label)}</span>'
        f"<span>{esc(value)}</span></div>"
        for i, (label, value, *_) in enumerate(rows)
    ]
    times = [round(ROW_START + i * 2.0, 3) for i in range(len(rows))]
    return SHORT.substitute(
        style=CARD_STYLE, kicker=esc(kicker), rows="\n".join(lines),
        row_times=json.dumps(times), clip=round(min(facts.short_seconds, SHORT_MAX), 3),
        duration=round(min(facts.short_seconds, SHORT_MAX) + SHORT_PAD, 3), lead=SHORT_LEAD,
        fade_at=round(min(facts.short_seconds, SHORT_MAX) + SHORT_PAD - FADE, 3),
    )  # fmt: skip


ROOT = Template("""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=1920, height=1080" />
    <script src="assets/gsap.min.js"></script>
    <style>
      * { margin: 0; padding: 0; box-sizing: border-box; }
      html, body { width: 1920px; height: 1080px; overflow: hidden; background: #0b1020; }
    </style>
  </head>
  <body>
    <div id="main" data-composition-id="main" data-start="0" data-duration="$duration"
      data-width="1920" data-height="1080">
$hosts
    </div>
    <script>
      window.__timelines["main"] = gsap.timeline({ paused: true });
    </script>
  </body>
</html>
""")


def build_project(run: Path, project: Path) -> Path:
    facts = run_facts(run)
    chs = chapters(facts)
    (project / "compositions").mkdir(parents=True, exist_ok=True)
    (project / "assets").mkdir(exist_ok=True)
    shutil.copyfile(REPO / "hyperframes" / "captions" / "assets" / "gsap.min.js",
                    project / "assets" / "gsap.min.js")  # fmt: skip
    shutil.copyfile(facts.short, project / "assets" / "short.mp4")
    hosts = []
    for number, ch in enumerate(chs, start=1):
        kicker = f"{number} / {len(chs)}"
        if ch.name == "replay":
            html = replay_html(facts)
        elif ch.name == "short":
            html = short_html(facts, kicker)
        else:
            title, sub, rows, step = CARDS[ch.name]
            first = "first" if ch.start == 0 else ""  # frame 0 is the thumbnail: title on
            html = card_html(ch.name, title, rows(facts), sub=sub, kicker=kicker, step=step,
                             duration=ch.duration, variant=first)  # fmt: skip
        (project / "compositions" / f"{ch.name}.html").write_text(html, encoding="utf-8")
        hosts.append(
            f'      <div id="{ch.name}" data-composition-id="{ch.name}" '
            f'data-composition-src="compositions/{ch.name}.html" data-start="{ch.start:g}" '
            f'data-duration="{ch.duration:g}" data-track-index="1" data-track-kind="graphics" '
            'data-width="1920" data-height="1080"></div>'
        )
    total = round(chs[-1].start + chs[-1].duration, 2)
    (project / "index.html").write_text(
        ROOT.substitute(duration=f"{total:g}", hosts="\n".join(hosts)), encoding="utf-8"
    )
    (project / "hyperframes.json").write_text(json.dumps({"paths": {"assets": "assets"}}),
                                              encoding="utf-8")  # fmt: skip
    (project / "meta.json").write_text(json.dumps({"id": "walkthrough", "name": "walkthrough"}),
                                       encoding="utf-8")  # fmt: skip
    return project


def npx() -> str:
    found = shutil.which("npx")
    if found is None:
        raise SystemExit("npx not found: HyperFrames needs Node.js 22+")
    return found


def render(project: Path, out: Path, teaser: tuple[float, float]) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    full = project.parent / f"{project.name}-1080.mp4"
    mp4, gif = out / "walkthrough.mp4", out / "demo.gif"
    hf = [npx(), "--yes", f"hyperframes@{HYPERFRAMES_VERSION}"]
    for step in (
        ["lint", str(project)],
        ["render", str(project), "--output", str(full), "--quality", "delivery"],
    ):
        result = run_command([*hf, *step], HYPERFRAMES_ENV)
        if result.returncode != 0:
            raise SystemExit(f"hyperframes {step[0]} failed:\n{result.stdout}\n{result.stderr}")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(full), "-vf", "scale=1280:720",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "30", "-pix_fmt", "yuv420p",
                    "-an", "-map_metadata", "-1", "-movflags", "+faststart", str(mp4)],
                   check=True)  # fmt: skip
    start, end = teaser
    palette = "fps=10,scale=1200:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];" \
              "[b][p]paletteuse=dither=bayer:bayer_scale=4"  # fmt: skip
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:g}", "-t", f"{end - start:g}",
                    "-i", str(full), "-filter_complex", palette, str(gif)],
                   check=True)  # fmt: skip
    return mp4, gif


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the walkthrough from a dry run.")
    parser.add_argument("run", type=Path, nargs="?", help="a media/dry-run/<run_id> folder")
    parser.add_argument("--out", type=Path, default=REPO / "docs" / "media")
    parser.add_argument("--work", type=Path, default=REPO / "media" / "work" / "walkthrough")
    args = parser.parse_args()
    run = args.run or max((REPO / "media" / "dry-run").iterdir(), key=lambda p: p.stat().st_mtime)
    if args.work.exists():
        shutil.rmtree(args.work)
    project = build_project(run, args.work)
    mp4, gif = render(project, args.out, teaser_window(chapters(run_facts(run))))
    for path in (mp4, gif):
        print(f"{path} ({path.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
