"""scripts/make_demo.py: the replay's timing comes from the run's audit log, and the long
render is the only part shown faster (and says so). The walkthrough (P1.8, SRD §1.2: a 2-3
minute video produced by the engine itself) is built from one dry run's own database and
files, and model-written text is escaped before it enters the composition."""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo.py"
CHAPTERS = ["intro", "replay", "brief", "script", "edit_plan", "short", "qa", "after", "guards",
            "outro"]  # fmt: skip


@pytest.fixture(scope="module")
def demo():
    spec = importlib.util.spec_from_file_location("make_demo", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def test_only_the_long_render_gets_a_sped_up_bar(demo):
    rows = [("get_state", "ok", 0), ("render_post", "ok", 60_000), ("create_draft", "ok", 16)]
    events, end = demo.tool_timeline(rows)
    assert [e["at"] for e in events] == pytest.approx([2.1, 2.42, 7.74])
    assert [e["bar"] for e in events] == [0, 5.0, 0]
    assert events[1]["speedup"] == pytest.approx(12.0) and events[1]["took"] == "60.0 s"
    assert events[2]["took"] == "16 ms" and end == pytest.approx(8.06)


def test_summary_lists_what_the_run_wrote(demo, tmp_path):
    run = tmp_path / "dry-1"
    (run / "renders" / "selfhelp-1-1").mkdir(parents=True)
    report = {"identity_results": [{"posts_drafted": 1}]}
    (run / "run_report.json").write_text(json.dumps(report), encoding="utf-8")
    lines = demo.summary_lines(run)
    assert lines[0] == "Run dry-1 wrote (1 fake Postiz draft):"
    assert [Path(line.strip()).name for line in lines[1:]] == [
        "selfhelp-1-1.mp4", "render_report.json", "selfhelp-1-1.otio", "selfhelp-1-1.fcpxml",
        "run_report.json",
    ]  # fmt: skip


SCHEMA = """
CREATE TABLE briefs (id INTEGER PRIMARY KEY, topic TEXT, angle TEXT, hook TEXT, cta TEXT,
  difference_note TEXT, exploration INTEGER);
CREATE TABLE scripts (id INTEGER PRIMARY KEY, brief_id INTEGER, version INTEGER, body TEXT);
CREATE TABLE edit_plans (id INTEGER PRIMARY KEY, brief_id INTEGER, plan TEXT);
CREATE TABLE renders (id INTEGER PRIMARY KEY, edit_plan_id INTEGER, path TEXT);
CREATE TABLE assets (id INTEGER PRIMARY KEY, rights_type TEXT);
CREATE TABLE qa_results (id INTEGER PRIMARY KEY, render_id INTEGER, check_name TEXT,
  result TEXT);
CREATE TABLE posts (id INTEGER PRIMARY KEY, render_id INTEGER, state TEXT, title TEXT,
  hashtags TEXT);
CREATE TABLE metrics_snapshots (post_id INTEGER, at TEXT, views INTEGER, likes INTEGER);
CREATE TABLE tool_calls (id INTEGER PRIMARY KEY, tool TEXT, status TEXT, duration_ms INTEGER);
"""
BEATS = [{"text": "Most plans fail before they start.", "target_s": 5.0},
         {"text": "So shrink the first step.", "target_s": 4.0}]  # fmt: skip


def add_render(conn, run, n, folder, *, hook, beats, checks, short_seconds, post=None):
    """One identity's brief, script, plan and render (all with id n), its folder, QA rows and,
    when `post` is (state, title, hashtags, views), its post and 24 h snapshot."""
    render = run / "renders" / folder
    render.mkdir(parents=True)
    (render / f"{folder}.mp4").write_bytes(f"not really mp4 {n}".encode())
    report = {
        "duration": short_seconds,
        "video": {"codec": "h264", "width": 1080, "height": 1920, "fps": 30.0},
        "audio": {"codec": "aac", "sample_rate": 48000},
        "loudness": {"integrated_lufs": None, "true_peak_dbtp": None, "target_lufs": -14.0},
        "tiers": {"ai_generated": 28.8, "own": 6.0},
        "film": {"seconds": 0.0},
        "people": {"none": [5, 1]},
        "captions": {"words": 87, "groups": 25, "coverage": 1.0},
        "exports": {"otio": f"{folder}.otio", "fcpxml": f"{folder}.fcpxml"},
        "look": {"id": "selfhelp", "version": 1},
    }
    (render / "render_report.json").write_text(json.dumps(report), encoding="utf-8")
    plan = {
        "timeline": [
            {"asset_id": 5, "in": 0.0, "out": 6.5, "role": "avatar"},
            {"asset_id": 1, "in": 0.0, "out": 2.0, "role": "broll"},
        ],
        "audio": {"voice": [{"asset_id": 4}], "music": None, "sfx": []},
        "overlays": [],
        "captions": {"block": "captions"},
        "look": {"id": "selfhelp", "version": 1},
    }
    conn.execute(
        "INSERT INTO briefs VALUES (?, 'Make the first step small', 'craft', ?, 'follow', "
        "'a sample post', 0)",
        (n, hook),
    )
    conn.execute("INSERT INTO scripts VALUES (?, ?, 1, ?)", (n, n, json.dumps({"beats": beats})))
    conn.execute("INSERT INTO edit_plans VALUES (?, ?, ?)", (n, n, json.dumps(plan)))
    conn.execute("INSERT INTO renders VALUES (?, ?, ?)", (n, n, str(render / f"{folder}.mp4")))
    conn.executemany(
        "INSERT INTO qa_results (render_id, check_name, result) VALUES (?, ?, ?)",
        [(n, name, result) for name, result in checks],
    )
    if post:
        state, title, hashtags, views = post
        conn.execute("INSERT INTO posts VALUES (?, ?, ?, ?, ?)",
                     (n, n, state, title, json.dumps(hashtags)))  # fmt: skip
        if views is not None:
            conn.execute("INSERT INTO metrics_snapshots VALUES (?, '24h', ?, 9)", (n, views))


def make_run(
    tmp_path,
    *,
    drafted=True,
    short_seconds=34.8,
    title="Make the first step small",
    hashtags=("#habits",),
    beats=BEATS,
    second=False,
    renders=True,
) -> Path:
    """A dry-run folder with only what the walkthrough reads, keyed as in the real DB. With
    `second`, another identity's render sorts first on disk but comes second by id, and its
    snapshot is stored first."""
    run = tmp_path / "dry-20261114-131539"
    run.mkdir()
    run_report = {"identity_results": [{"posts_drafted": int(drafted)}]}
    (run / "run_report.json").write_text(json.dumps(run_report), encoding="utf-8")
    conn = sqlite3.connect(run / "engine.db")
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO assets VALUES (?, ?)",
                     [(1, "own"), (4, "ai_generated"), (5, "ai_generated")])  # fmt: skip
    conn.executemany(
        "INSERT INTO tool_calls (tool, status, duration_ms) VALUES (?, ?, ?)",
        [("mcp__engine__get_state", "ok", 3), ("mcp__engine__render_post", "ok", 60_000),
         ("mcp__engine__create_draft", "ok" if drafted else "denied", 16)],
    )  # fmt: skip
    if second:
        conn.execute("INSERT INTO metrics_snapshots VALUES (2, '24h', 999, 9)")
    if renders:
        checks = [("rights", "pass"), ("claims_about_abbas", "pass"), ("qa_review", "pass")]
        post = ("published", title, list(hashtags), 120) if drafted else None
        add_render(conn, run, 1, "selfhelp-1-1", hook="Most plans fail before they start",
                   beats=beats, checks=checks, short_seconds=short_seconds, post=post)  # fmt: skip
    if second:
        add_render(conn, run, 2, "anxiety-2-1", hook="Other identity", beats=BEATS,
                   checks=[("rights", "fail"), ("spec", "pass")], short_seconds=20.0,
                   post=("drafted", "Other post", ["#other"], None))  # fmt: skip
    conn.commit()
    conn.close()
    return run


def test_run_facts_reads_the_run(demo, tmp_path):
    facts = demo.run_facts(make_run(tmp_path))
    assert facts.run_id == "dry-20261114-131539"
    assert facts.brief["hook"] == "Most plans fail before they start"
    assert facts.beats == [(0.0, "Most plans fail before they start."),
                           (5.0, "So shrink the first step.")]  # fmt: skip
    assert facts.shots == [("avatar", "ai_generated", 6.5), ("broll", "own", 2.0)]
    assert facts.checks == [("rights", "pass"), ("claims_about_abbas", "pass"),
                            ("qa_review", "pass")]  # fmt: skip
    assert facts.post == {"state": "published", "title": "Make the first step small",
                          "hashtags": ["#habits"]}  # fmt: skip
    assert (facts.views_24h, facts.likes_24h) == (120, 9)
    assert (facts.tool_calls, facts.tool_calls_ok) == (3, 3)
    assert facts.short.name == "selfhelp-1-1.mp4" and facts.short_seconds == 34.8


def test_facts_without_a_draft(demo, tmp_path):
    facts = demo.run_facts(make_run(tmp_path, drafted=False))
    assert facts.post is None and facts.views_24h is None
    assert (facts.tool_calls, facts.tool_calls_ok) == (3, 2)
    assert [c.name for c in demo.chapters(facts)] == CHAPTERS


def test_chapters_last_two_to_three_minutes(demo, tmp_path):
    chapters = demo.chapters(demo.run_facts(make_run(tmp_path)))
    assert [c.name for c in chapters] == CHAPTERS
    assert chapters[0].start == 0
    for before, after in zip(chapters, chapters[1:], strict=False):
        assert after.start == pytest.approx(before.start + before.duration)
    total = chapters[-1].start + chapters[-1].duration
    assert 120 <= total <= 180


def test_the_short_chapter_is_capped(demo, tmp_path):
    chapters = demo.chapters(demo.run_facts(make_run(tmp_path, short_seconds=95.0)))
    short = next(c for c in chapters if c.name == "short")
    assert short.duration == pytest.approx(demo.SHORT_MAX + demo.SHORT_PAD)
    assert chapters[-1].start + chapters[-1].duration <= 180


def test_card_text_is_escaped(demo):
    html = demo.card_html("brief", "Brief", [("hook", "<b>a</b> & </template>")])
    assert "&lt;b&gt;a&lt;/b&gt; &amp; &lt;/template&gt;" in html
    assert html.count("</template>") == 1 and html.rstrip().endswith("</template>")


def test_cards_hold_long_lines_long_enough_to_read(demo):
    """About 0.5 s plus 0.3 s a word after a line appears, before the card fades."""
    rows = [("", "short line"), ("", " ".join(["word"] * 24))]
    seconds = demo.card_seconds(rows, step=1.2)
    last_shown = demo.ROW_START + 1.2
    assert seconds - demo.FADE >= last_shown + 0.5 + 0.3 * 24


def test_teaser_window_is_the_replay(demo, tmp_path):
    chapters = demo.chapters(demo.run_facts(make_run(tmp_path)))
    replay = next(c for c in chapters if c.name == "replay")
    assert demo.teaser_window(chapters) == (replay.start, replay.start + replay.duration)


def test_build_project_mounts_every_chapter(demo, tmp_path):
    run = make_run(tmp_path)
    project = demo.build_project(run, tmp_path / "project")
    index = (project / "index.html").read_text(encoding="utf-8")
    for chapter in demo.chapters(demo.run_facts(run)):
        assert f'data-composition-src="compositions/{chapter.name}.html"' in index
        assert f'data-start="{chapter.start:g}"' in index
        assert (project / "compositions" / f"{chapter.name}.html").is_file()
    qa = (project / "compositions" / "qa.html").read_text(encoding="utf-8")
    assert "Claims about the presenter" in qa and "claims_about_abbas" not in qa
    intro = (project / "compositions" / "intro.html").read_text(encoding="utf-8")
    assert "dry-20261114-131539" in intro and "faked" in intro
    assert (project / "assets" / "short.mp4").read_bytes() == b"not really mp4 1"


def test_two_identities_are_not_mixed(demo, tmp_path):
    """Facts follow render -> edit plan -> brief -> script, post, QA rows and snapshot."""
    facts = demo.run_facts(make_run(tmp_path, second=True))
    assert facts.short.parent.name == "selfhelp-1-1"
    assert facts.brief["hook"] == "Most plans fail before they start"
    assert facts.checks == [("rights", "pass"), ("claims_about_abbas", "pass"),
                            ("qa_review", "pass")]  # fmt: skip
    assert facts.post["title"] == "Make the first step small"
    assert facts.views_24h == 120


def test_a_run_without_a_render_says_so(demo, tmp_path):
    with pytest.raises(SystemExit, match="no render"):
        demo.run_facts(make_run(tmp_path, renders=False))


def test_model_text_is_escaped_in_every_chapter(demo, tmp_path):
    hostile = "<script>alert(1)</script>"
    beats = [{"text": f"Close the scene </template> {hostile}", "target_s": 5.0}]
    run = make_run(tmp_path, title=hostile, hashtags=["#a<b>"], beats=beats)
    project = demo.build_project(run, tmp_path / "project")
    for page in (project / "compositions").glob("*.html"):
        html = page.read_text(encoding="utf-8")
        assert "<script>alert" not in html, page.name
        assert html.count("</template>") == 1, page.name
    after = (project / "compositions" / "after.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in after and "#a&lt;b&gt;" in after
