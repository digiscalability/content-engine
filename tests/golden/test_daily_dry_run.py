"""P0.7 done-when: `ce daily --dry-run` produces a brief, a script, an edit plan and an MP4 on
fakes, with its render report, FCPXML and OTIO, then passes the QA gate to a fake Postiz
draft, all through the agent tools and their guards. Runs ffmpeg (NVENC) and HyperFrames
for real, about a minute: `uv run pytest -m golden`."""

import json
import shutil
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import opentimelineio as otio
import pytest

from engine.agents.dryrun import BEATS, daily_dry_run
from engine.config import load_config
from engine.db import connect
from engine.produce.ffmpeg import probe

from . import media

pytestmark = pytest.mark.golden

CAPTION_BAND = "crop=1080:240:0:1060"  # the caption line sits 650 px above the bottom
SPOKEN = " ".join(b["text"] for b in BEATS)
VOICE_S = len(SPOKEN.split()) / 2.5  # FakeVoice speaks 150 words a minute


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    for tool in ("ffmpeg", "ffprobe", "npx"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not on PATH")
    root = tmp_path_factory.mktemp("dry run")  # a space, like real folders
    return daily_dry_run(load_config(), root=root, run_id="dry-golden")


@pytest.fixture(scope="module")
def db(result):
    return connect(result.root / "engine.db")


@pytest.fixture(scope="module")
def made(result):
    (only,) = result.produced
    return only


def count(db, table: str) -> int:
    return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_the_run_stores_a_brief_a_script_an_edit_plan_and_a_render(made, db):
    assert [count(db, t) for t in ("briefs", "scripts", "edit_plans", "renders")] == [1, 1, 1, 1]
    assert (made.brief_id, made.script_id, made.edit_plan_id, made.render_id) == (1, 1, 1, 1)


def test_every_step_went_through_the_audited_tools(db):
    rows = db.execute("SELECT tool, status FROM tool_calls ORDER BY id").fetchall()
    tools = [r["tool"].removeprefix("mcp__engine__") for r in rows]
    assert tools == [
        "get_state", "get_playbook", "get_style_profile", "search_library", "submit_brief",
        "submit_script", "budget_status", "synthesize_voice", "request_avatar",
        "submit_edit_plan", "render_post", "run_qa_checks", "get_render_report",
        "submit_qa_review", "create_draft",
    ]  # fmt: skip
    assert {r["status"] for r in rows} == {"ok"}


def test_the_mp4_meets_fr_prd_5(made):
    info = probe(Path(made.mp4))
    assert info.duration == pytest.approx(VOICE_S, abs=0.05)
    assert (info.video_codec, info.width, info.height, info.fps) == ("h264", 1080, 1920, 30.0)
    assert info.pix_fmt == "yuv420p" and (info.audio_codec, info.sample_rate) == ("aac", 48000)
    assert {"encoder", "creation_time", "location"}.isdisjoint(info.tags), info.tags


def white_pixels(mp4: Path, at: float) -> int:
    rgb = media.raw_frame(mp4, at, CAPTION_BAND, "rgb24")
    return sum(1 for i in range(0, len(rgb), 3) if min(rgb[i : i + 3]) > 225)


def test_captions_follow_the_script_on_screen(made):
    mp4 = Path(made.mp4)
    words = json.loads((mp4.parent / "words.json").read_text("utf-8"))
    assert [w["text"] for w in words] == SPOKEN.split()
    assert white_pixels(mp4, 0.03) == 0  # nothing before the first caption at 0.1 s
    assert white_pixels(mp4, 3.0) > 500  # the presenter shot, mid-sentence


def test_the_render_report_says_what_the_render_contains(made):
    report = json.loads(Path(made.report).read_text("utf-8"))
    assert Path(made.report).parent == Path(made.mp4).parent  # one folder per render
    assert report["render_id"] == 1 and report["file"] == Path(made.mp4).name
    assert set(report["tiers"]) == {"ai_generated", "own"}  # avatar, fixture cutaways
    assert sum(report["tiers"].values()) == pytest.approx(VOICE_S, abs=0.01)
    assert report["film"]["seconds"] == 0 and report["ai_generated_on_screen"] is True
    assert set(report["people"]) == {"none"}
    assert (report["video"]["width"], report["video"]["height"]) == (1080, 1920)
    assert report["loudness"]["integrated_lufs"] is None  # the fake voice is silent
    assert report["captions"]["words"] == len(SPOKEN.split())
    assert report["captions"]["coverage"] >= 0.95  # FR-QA-4's bar
    assert report["exports"] == {"otio": Path(made.exports["otio"]).name,
                                 "fcpxml": Path(made.exports["fcpxml"]).name}  # fmt: skip


def test_the_exports_open_with_the_same_cut(made):
    frames = round(VOICE_S * 30)
    timeline = otio.adapters.read_from_file(made.exports["otio"])
    picture = next(t for t in timeline.tracks if t.name == "V1 picture")
    assert picture.duration().to_frames() == frames
    assert all(Path(otio.url_utils.filepath_from_url(c.media_reference.target_url)).is_file()
               for c in picture)  # fmt: skip
    sequence = ET.parse(made.exports["fcpxml"]).getroot().find("library/event/project/sequence")
    assert Fraction(sequence.get("duration").removesuffix("s")) * 30 == frames
    assert len(sequence.find("spine")) == len(picture)


def test_the_qa_gate_passed_and_a_fake_draft_was_made(made, db):
    results = {r["check_name"]: r["result"] for r in db.execute("SELECT * FROM qa_results")}
    assert results == dict.fromkeys(
        ["rights", "disclosure", "banned_topics", "claims_about_abbas", "qa_review"], "pass"
    )
    post = db.execute("SELECT state, render_id, ai_disclosure, postiz_id FROM posts").fetchone()
    assert tuple(post) == ("drafted", 1, 1, made.drafts[0])


def test_the_run_report_names_the_draft(result):
    report = json.loads((result.root / "run_report.json").read_text("utf-8"))
    assert report["run_id"] == "dry-golden" and report["needs_abbas"] == []
    assert report["budget_used"]["elevenlabs_chars"] == len(SPOKEN)
    assert report["identity_results"] == [
        {"identity": "selfhelp", "posts_drafted": 1, "posts_failed": [], "qa_holds": []}
    ]
    assert any("scripted (pass)" in w for w in report["warnings"])
