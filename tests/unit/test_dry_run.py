"""The dry run's parts that need no render: its config, its own DB, the scripted content and
the director's edit plan. The whole run is the golden test `test_daily_dry_run.py`."""

import re
import subprocess
from pathlib import Path

import pytest

from engine.agents import dryrun
from engine.agents.schemas import FORMAT_SECONDS, BriefSubmission, ScriptSubmission
from engine.agents.tools import call_tool
from engine.config import load_config
from engine.produce.editplan import EditPlan


def fake_run(argv, env=None):
    Path(argv[-1]).write_bytes(f"clip {argv[-1]}".encode())  # distinct bytes per file
    return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def config(repo_root):
    return load_config(repo_root / "config")


def test_dry_run_config_uses_fake_vendor_ids(config):
    selfhelp = dryrun.dry_run_config(config).identities["selfhelp"]
    assert selfhelp.voice_id and selfhelp.avatar_id
    assert [c.postiz_integration_id for c in selfhelp.channels] == ["yt-1"]
    assert config.identities["selfhelp"].voice_id == ""  # the real config is untouched


def test_each_dry_run_has_its_own_db(config, tmp_path):
    first = dryrun.start_dry_run(config, tmp_path, "dry-1", run=fake_run)
    call_tool(first, "submit_brief", dryrun.brief_for("selfhelp"))
    second = dryrun.start_dry_run(config, tmp_path, "dry-2", run=fake_run)
    assert second.media_root == tmp_path / "dry-2"
    assert second.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert first.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1


def test_the_fixture_library_is_rights_clean_cutaway_footage(config, tmp_path):
    ctx = dryrun.start_dry_run(config, tmp_path, "dry-1", run=fake_run)
    results = call_tool(ctx, "search_library", {"format": "C"})["results"]
    assert len(results) == len(dryrun.FIXTURE_CLIPS)
    assert {r["rights_type"] for r in results} == {"own"}
    assert {r["media_kind"] for r in results} == {"video"}


def test_the_director_briefs_from_the_topic_candidates(config, tmp_path):
    ctx = dryrun.start_dry_run(config, tmp_path, "dry-1", run=fake_run)
    plan = call_tool(ctx, "get_topic_candidates", {"identity": "selfhelp"})
    brief = dryrun.brief_for("selfhelp", plan)
    assert brief["topic"] == plan["candidates"][0]["topic"]
    assert (brief["format"], brief["exploration"]) == (plan["format"], plan["exploration"])
    assert call_tool(ctx, "submit_brief", brief)["ok"]


def test_the_scripted_post_obeys_the_rules():
    brief = BriefSubmission.model_validate(dryrun.brief_for("selfhelp"))
    script = ScriptSubmission.model_validate({"brief_id": 1, "beats": dryrun.BEATS})
    low, high = FORMAT_SECONDS["C"]
    assert brief.format == "C" and low <= script.duration <= high
    voiced = script.words / 2.5  # FakeVoice speaks 150 words a minute
    assert low <= voiced <= high
    first_person = re.compile(r"\b(I|me|my|we|our)\b", re.IGNORECASE)  # FR-QA-8
    assert not first_person.search(script.spoken_text)


def test_director_plan_keeps_avatar_in_sync():
    cutaways = [(11, 4.0), (12, 4.0), (13, 4.0)]
    plan = EditPlan.model_validate(
        dryrun.director_plan(voice=5, avatar=6, duration=35.2, cutaways=cutaways, look="selfhelp")
    )
    assert plan.duration == pytest.approx(35.2)
    starts = plan.shot_starts()
    for start, shot in zip(starts, plan.timeline, strict=True):
        if shot.role == "avatar":
            assert shot.in_s == pytest.approx(start)  # lip sync: source time = timeline time
    avatar_runs = [s.duration for s in plan.timeline if s.role == "avatar"]
    assert max(avatar_runs) <= 8.0  # cut away every 5 to 8 s (format C)
    assert {s.asset_id for s in plan.timeline if s.role == "broll"} == {11, 12, 13}
    assert plan.timeline[-1].role == "avatar"
    assert plan.audio.voice[0].out_s == pytest.approx(35.2) and plan.captions is not None
