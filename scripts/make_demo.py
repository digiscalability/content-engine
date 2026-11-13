"""The README demo (SRD P0.7b): a replay of a real `ce daily --dry-run`, rendered with
HyperFrames. Local only.

    uv run python scripts/make_demo.py [<dry-run folder>] [--out docs/media]

It reads the run's own audit log (`tool_calls`: every tool, its status and duration), its
run report and its render folder, and builds a 1920x1080 composition: the terminal
replays the tool calls with their real durations, sped up while the render runs and
labelled as a replay, and beside it plays the Short the run made. Writes `demo.mp4` and a
smaller `demo.gif` for the README.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from string import Template

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from engine.produce.overlay import HYPERFRAMES_ENV, HYPERFRAMES_VERSION  # noqa: E402
from engine.produce.process import run_command  # noqa: E402

FPS = 30
DURATION = 24.0
TYPE_START, CHAR_STEP = 0.6, 0.045
LINES_START, LINE_STEP = 2.1, 0.32
RENDER_SHOWN = 5.0  # seconds of playback for the long render
SUMMARY_STEP = 0.25
COMMAND = "uv run ce daily --dry-run"


def seconds_label(ms: int) -> str:
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


def tool_rows(db: Path) -> list[tuple[str, str, int]]:
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT tool, status, duration_ms FROM tool_calls ORDER BY id").fetchall()
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
      <div id="demo" data-composition-id="demo" data-composition-src="compositions/demo.html"
        data-start="0" data-duration="$duration" data-track-index="1" data-track-kind="graphics"
        data-width="1920" data-height="1080"></div>
    </div>
    <script>
      window.__timelines["main"] = gsap.timeline({ paused: true });
    </script>
  </body>
</html>
""")

SCENE = Template("""<template>
  <style>
    #root { position: absolute; inset: 0; background: #0b1020; color: #e2e8f0;
            font-family: "Inter", sans-serif; }
    #title { position: absolute; left: 60px; top: 40px; font-size: 44px; font-weight: 700; }
    #note { position: absolute; left: 60px; top: 100px; font-size: 25px; color: #94a3b8; }
    #term { position: absolute; left: 60px; top: 156px; width: 1250px; height: 896px;
            background: #111827; border: 2px solid #1f2937; border-radius: 18px; padding: 24px 28px;
            font-family: "JetBrains Mono", monospace; font-size: 24px; line-height: 35px; }
    .row { white-space: pre; opacity: 0; }
    .ok { color: #34d399; }
    .dim { color: #64748b; }
    .bar { display: inline-block; width: 220px; height: 12px; margin: 0 14px 2px 0;
           vertical-align: middle; background: #1f2937; border-radius: 6px; overflow: hidden; }
    .fill { display: block; width: 100%; height: 100%; background: #f59e0b;
            transform-origin: left center; transform: scaleX(0); }
    .ch { opacity: 0; }
    #phone { position: absolute; left: 1380px; top: 156px; width: 468px; height: 832px;
             border-radius: 34px; background: #000; border: 6px solid #1f2937; overflow: hidden;
             opacity: 0; }
    #short { width: 100%; height: 100%; object-fit: cover; }
    #phone-note { position: absolute; left: 1350px; top: 1004px; width: 530px; text-align: center;
                  font-size: 20px; line-height: 26px; color: #94a3b8; opacity: 0; }
  </style>
  <div id="root" data-composition-id="demo" data-width="1920" data-height="1080">
    <div id="title">content-engine: <span style="color:#f59e0b">ce daily --dry-run</span></div>
    <div id="note">Replay of a real run from its own audit log: the tool calls and timings are
      real; playback is sped up while the video renders.</div>
    <div id="term">
      <div class="row" id="prompt"><span class="dim">PS content-engine&gt; </span>$typed</div>
$rows
    </div>
    <div id="phone">
      <video id="short" class="clip" src="assets/short.mp4" data-start="$short_start"
        data-duration="$short_duration" data-track-index="2" muted playsinline></video>
    </div>
    <div id="phone-note">The Short it made, with every vendor faked: placeholder avatar,
      ffmpeg-drawn B-roll, captions.</div>
  </div>
  <script>
    const events = $events;
    const tl = gsap.timeline({ paused: true });
    tl.set("#prompt", { opacity: 1 }, 0.2);
    document.querySelectorAll("#prompt .ch").forEach((el, i) => {
      tl.set(el, { opacity: 1 }, $type_start + i * $char_step);
    });
    for (const e of events) {
      tl.set(e.row, { opacity: 1 }, e.at);
      if (e.fill) {
        tl.fromTo(e.fill, { scaleX: 0 }, { scaleX: 1, duration: e.bar, ease: "none" }, e.at);
        tl.set(e.done, { opacity: 1 }, e.at + e.bar);
      }
    }
    tl.to("#phone", { opacity: 1, duration: 0.4, ease: "power2.out" }, $short_start - 0.3);
    tl.to("#phone-note", { opacity: 1, duration: 0.4 }, $short_start);
    window.__timelines["demo"] = tl;
  </script>
</template>
""")


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_project(run: Path, project: Path) -> Path:
    rows = tool_rows(run / "engine.db")
    events, t = tool_timeline(rows)
    html_rows, js_events = [], []
    for i, e in enumerate(events):
        row_id = f"t{i}"
        mark = '<span class="ok">ok</span>' if e["status"] == "ok" else esc(e["status"])
        if e["bar"]:
            body = (
                f'{e["tool"]:<19}<span class="bar"><span class="fill" id="f{i}"></span>'
                f'</span><span id="d{i}" style="opacity:0">{mark} {e["took"]}'
                f'<span class="dim">  (shown {e["speedup"]:.0f}x faster)</span></span>'
            )
            js_events.append({"row": f"#{row_id}", "at": round(e["at"], 3), "bar": e["bar"],
                              "fill": f"#f{i}", "done": f"#d{i}"})  # fmt: skip
        else:
            body = f'{e["tool"]:<19}{mark} <span class="dim">{e["took"]:>8}</span>'
            js_events.append({"row": f"#{row_id}", "at": round(e["at"], 3)})
        html_rows.append(
            f'      <div class="row" id="{row_id}">  <span class="dim">&gt;</span> {body}</div>'
        )
    t += 0.3
    for j, line in enumerate(summary_lines(run)):
        html_rows.append(f'      <div class="row" id="s{j}">{esc(line)}</div>')
        js_events.append({"row": f"#s{j}", "at": round(t + j * SUMMARY_STEP, 3)})
    render_done = next(e["at"] + e["bar"] for e in events if e["bar"])
    short_start = round(render_done + 0.4, 3)
    typed = "".join(f'<span class="ch">{esc(c)}</span>' for c in COMMAND)

    (project / "compositions").mkdir(parents=True, exist_ok=True)
    (project / "assets").mkdir(exist_ok=True)
    shutil.copyfile(REPO / "hyperframes" / "captions" / "assets" / "gsap.min.js",
                    project / "assets" / "gsap.min.js")  # fmt: skip
    mp4 = next((run / "renders").glob("*/*.mp4"))
    shutil.copyfile(mp4, project / "assets" / "short.mp4")
    (project / "index.html").write_text(ROOT.substitute(duration=DURATION), encoding="utf-8")
    scene = SCENE.substitute(
        rows="\n".join(html_rows), events=json.dumps(js_events), typed=typed,
        type_start=TYPE_START, char_step=CHAR_STEP, short_start=short_start,
        short_duration=round(DURATION - short_start, 3),
    )  # fmt: skip
    (project / "compositions" / "demo.html").write_text(scene, encoding="utf-8")
    (project / "hyperframes.json").write_text(json.dumps({"paths": {"assets": "assets"}}),
                                              encoding="utf-8")  # fmt: skip
    (project / "meta.json").write_text(json.dumps({"id": "demo", "name": "demo"}), "utf-8")
    return project


def npx() -> str:
    found = shutil.which("npx")
    if found is None:
        raise SystemExit("npx not found: HyperFrames needs Node.js 22+")
    return found


def render(project: Path, out: Path) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    mp4, gif = out / "demo.mp4", out / "demo.gif"
    hf = [npx(), "--yes", f"hyperframes@{HYPERFRAMES_VERSION}"]
    for step in (
        ["lint", str(project)],
        ["render", str(project), "--output", str(mp4), "--quality", "delivery"],
    ):
        result = run_command([*hf, *step], HYPERFRAMES_ENV)
        if result.returncode != 0:
            raise SystemExit(f"hyperframes {step[0]} failed:\n{result.stdout}\n{result.stderr}")
    palette = "fps=10,scale=1200:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];" \
              "[b][p]paletteuse=dither=bayer:bayer_scale=4"  # fmt: skip
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp4), "-filter_complex", palette,
                    str(gif)], check=True)  # fmt: skip
    return mp4, gif


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the README demo from a dry run.")
    parser.add_argument("run", type=Path, nargs="?", help="a media/dry-run/<run_id> folder")
    parser.add_argument("--out", type=Path, default=REPO / "docs" / "media")
    parser.add_argument("--work", type=Path, default=REPO / "media" / "work" / "demo")
    args = parser.parse_args()
    run = args.run or max((REPO / "media" / "dry-run").iterdir(), key=lambda p: p.stat().st_mtime)
    if args.work.exists():
        shutil.rmtree(args.work)
    project = build_project(run, args.work)
    mp4, gif = render(project, args.out)
    for path in (mp4, gif):
        print(f"{path} ({path.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
