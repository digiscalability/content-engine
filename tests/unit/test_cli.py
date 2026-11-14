"""`ce` CLI wiring (SRD §5.2). Probes are faked, so this never depends on the machine."""

from contextlib import nullcontext

import pytest
from typer.testing import CliRunner

from engine import doctor
from engine.cli import app

runner = CliRunner()


def fake_real(missing=()):
    def which(name):
        return None if name in missing else f"C:/bin/{name}.exe"

    def run(argv):
        if "-encoders" in argv:
            return 0, "h264_nvenc"
        return 0, "v24.14.0" if argv[0].endswith("node.exe") else "tool 1.0"

    return lambda: doctor.Probes(which=which, run=run, disk_free_bytes=lambda p: 200 * doctor.GB)


@pytest.fixture
def fake_machine(monkeypatch):
    monkeypatch.setattr(doctor.Probes, "real", staticmethod(fake_real()))


def test_help_lists_doctor():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.output


def test_doctor_runs_and_exits_zero_on_fakes(fake_machine):
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "ANTHROPIC_API_KEY" in result.output
    assert "checks:" in result.output


def test_doctor_exits_one_when_required_tool_missing(monkeypatch):
    monkeypatch.setattr(doctor.Probes, "real", staticmethod(fake_real(missing={"ffprobe"})))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "[FAIL] tools  ffprobe" in result.output


def test_global_options_accepted(fake_machine):
    result = runner.invoke(app, ["--dry-run", "--identity", "selfhelp", "doctor"])
    assert result.exit_code == 0, result.output


def test_doctor_reports_broken_config(fake_machine, monkeypatch):
    from engine import cli
    from engine.config import ConfigError

    def broken():
        raise ConfigError(["sources.yaml: sources.0.rights_proof: Field required"])

    monkeypatch.setattr(cli, "load_config", broken)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "sources.yaml: sources.0.rights_proof: Field required" in result.output


def test_daily_without_dry_run_points_at_p09():
    result = runner.invoke(app, ["daily"])
    assert result.exit_code == 2 and "P0.9" in result.output


def fake_dry_run(tmp_path, seen, failed=()):
    from engine.agents.dryrun import DryRunResult, Produced
    from engine.agents.schemas import BudgetUsed, FailedPost, IdentityResult, RunReport

    def run(config, identity=None, **kwargs):
        seen["identity"] = identity
        report = RunReport(
            run_id="dry-x",
            identity_results=[
                IdentityResult(
                    identity="selfhelp",
                    posts_failed=[FailedPost(post="selfhelp render", reason=r) for r in failed],
                )
            ],
            budget_used=BudgetUsed(elevenlabs_chars=420),
            warnings=["dry run: every vendor is faked"],
            needs_abbas=[],
        )
        made = Produced("selfhelp", 1, 2, 3, 4, str(tmp_path / "selfhelp-1-3.mp4"))
        return DryRunResult("dry-x", tmp_path, report, [made])

    return run


@pytest.mark.parametrize(
    "argv",
    [
        ["daily", "--dry-run", "--identity", "selfhelp"],
        ["--dry-run", "--identity", "selfhelp", "daily"],
    ],
)
def test_daily_dry_run_reports_what_it_made(monkeypatch, tmp_path, argv):
    from engine import cli

    seen = {}
    monkeypatch.setattr(cli, "daily_dry_run", fake_dry_run(tmp_path, seen))
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert seen["identity"] == "selfhelp"
    assert "brief 1, script 2, edit plan 3, render 4" in result.output
    assert "selfhelp-1-3.mp4" in result.output and "run_report.json" in result.output


def test_daily_dry_run_exits_non_zero_when_a_post_failed(monkeypatch, tmp_path):
    from engine import cli

    monkeypatch.setattr(cli, "daily_dry_run", fake_dry_run(tmp_path, {}, failed=["boom"]))
    result = runner.invoke(app, ["daily", "--dry-run"])
    assert result.exit_code == 1 and "boom" in result.output


def test_refs_analyze_reports_the_profile_and_what_it_deleted(monkeypatch, tmp_path):
    from engine import cli
    from engine.db import open_db

    seen = {}

    def fake_analyze(conn, identity, folder, measure, keep=False):
        seen.update(identity=identity, folder=folder, keep=keep)
        metrics = {"cuts_per_min": 18.0, "first_word_s": 0.4, "tempo_bpm": 100.0}
        return {"profile_id": 7, "ref_count": 2, "palette": ["#101010"], "metrics": metrics}

    monkeypatch.setattr(cli, "analyze_refs", fake_analyze)
    monkeypatch.setattr(cli, "GpuLock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(cli, "open_db", lambda path: open_db(tmp_path / "engine.db"))
    monkeypatch.setattr(cli, "reference_files", lambda folder: ["a.mp4", "b.mp4"])
    result = runner.invoke(app, ["refs", "analyze", "selfhelp"])
    assert result.exit_code == 0, result.output
    first = result.output.splitlines()[0]
    assert "Measuring 2 references" in first and "deleted once" in first  # said before
    assert seen["identity"] == "selfhelp" and seen["keep"] is False
    assert seen["folder"].as_posix().endswith("references/inbox/selfhelp")
    assert "profile 7 from 2 references" in result.output
    assert "Deleted the 2 reference files (C10)" in result.output


def test_refs_analyze_with_an_empty_inbox_points_at_g0_8(monkeypatch, tmp_path):
    from engine import cli
    from engine.db import open_db

    def empty(conn, identity, folder, measure, keep=False):
        raise ValueError("no reference videos in x: drop 5-10 shorts there first (G0.8)")

    monkeypatch.setattr(cli, "analyze_refs", empty)
    monkeypatch.setattr(cli, "GpuLock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(cli, "open_db", lambda path: open_db(tmp_path / "engine.db"))
    result = runner.invoke(app, ["refs", "analyze", "selfhelp"])
    assert result.exit_code == 1 and "G0.8" in result.output


def test_refs_analyze_refuses_an_unknown_identity():
    result = runner.invoke(app, ["refs", "analyze", "nobody"])
    assert result.exit_code == 2 and "nobody" in result.output


def test_look_propose_without_a_profile_points_at_refs_analyze(monkeypatch, tmp_path):
    from engine import cli
    from engine.db import open_db

    monkeypatch.setattr(cli, "open_db", lambda path: open_db(tmp_path / "engine.db"))
    result = runner.invoke(app, ["look", "propose", "selfhelp"])
    assert result.exit_code == 1 and "ce refs analyze selfhelp" in result.output


def test_look_propose_prints_the_three_samples(monkeypatch, tmp_path):
    from engine import cli
    from engine.db import open_db
    from engine.style.look import LookConfig
    from engine.style.propose import Proposal

    def fake_propose(config, conn, identity, proposer, root, count=3, **kwargs):
        looks = [LookConfig(id=identity, version=n, name=f"Look {n}") for n in (1, 2, 3)]
        samples = [tmp_path / f"look-{n}.mp4" for n in (1, 2, 3)]
        return Proposal(tmp_path / "proposal-1", looks, samples, tmp_path / "contact.png")

    monkeypatch.setattr(cli, "propose_looks", fake_propose)
    monkeypatch.setattr(cli, "open_db", lambda path: open_db(tmp_path / "engine.db"))
    result = runner.invoke(app, ["look", "propose", "selfhelp"])
    assert result.exit_code == 0, result.output
    assert "3: Look 3" in result.output and "look-2.mp4" in result.output
    assert "ce look approve selfhelp <n>" in result.output


def test_look_approve_saves_the_chosen_look(monkeypatch, tmp_path):
    from engine import cli
    from engine.db import open_db

    seen = {}

    def fake_approve(config_dir, conn, identity, proposal, number):
        seen.update(identity=identity, proposal=proposal, number=number)
        return config_dir / "looks" / "selfhelp-v1.yaml"

    proposal = tmp_path / "proposal-x"
    proposal.mkdir()
    monkeypatch.setattr(cli, "approve_look", fake_approve)
    monkeypatch.setattr(cli, "open_db", lambda path: open_db(tmp_path / "engine.db"))
    result = runner.invoke(app, ["look", "approve", "selfhelp", "2", "--proposal", str(proposal)])
    assert result.exit_code == 0, result.output
    assert seen == {"identity": "selfhelp", "proposal": proposal, "number": 2}
    assert "selfhelp-v1.yaml" in result.output and "active look" in result.output


def test_refs_analyze_reports_a_busy_gpu(monkeypatch, tmp_path):
    from engine import cli
    from engine.db import open_db
    from engine.ops.gpu_lock import GpuBusy

    class Busy:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            raise GpuBusy("the GPU is busy (render edit plan 3 pid 1); waited 3600 s")

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cli, "GpuLock", Busy)
    monkeypatch.setattr(cli, "open_db", lambda path: open_db(tmp_path / "engine.db"))
    result = runner.invoke(app, ["refs", "analyze", "selfhelp"])
    assert result.exit_code == 1 and "GPU is busy" in result.output


def digest_world(monkeypatch, tmp_path, drafted=True):
    from engine import cli
    from engine.agents.state import sync_identities
    from engine.db import open_db
    from engine.ops.notify import FakeNotifier

    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)  # db, reports and work under tmp_path
    conn = open_db(tmp_path / "data" / "engine.db")
    sync_identities(conn, cli.load_config())
    if drafted:
        brief = conn.execute(
            "INSERT INTO briefs (identity_id, format, topic, angle, hook, cta, difference_note) "
            "VALUES ('selfhelp', 'C', 'starting small', 'a', 'h', 'follow', 'n')"
        ).lastrowid
        plan = conn.execute(
            "INSERT INTO edit_plans (brief_id, version, plan) VALUES (?, 1, '{}')", (brief,)
        ).lastrowid
        render = conn.execute(
            "INSERT INTO renders (edit_plan_id, path) VALUES (?, 'r.mp4')", (plan,)
        ).lastrowid
        conn.execute(
            "INSERT INTO posts (brief_id, channel_id, render_id, state, ai_disclosure, title, "
            "postiz_id) VALUES (?, 1, ?, 'drafted', 1, 'Start smaller', 'pz-1')",
            (brief, render),
        )
    notifier = FakeNotifier()
    monkeypatch.setattr(cli, "make_notifier", lambda: notifier)
    return notifier


def test_digest_writes_the_report_and_notifies_when_something_waits(monkeypatch, tmp_path):
    notifier = digest_world(monkeypatch, tmp_path)
    result = runner.invoke(app, ["digest", "--date", "2026-11-20"])
    assert result.exit_code == 0, result.output
    report = tmp_path / "reports" / "2026-11-20.md"
    assert report.is_file() and "Start smaller" in report.read_text("utf-8")
    assert notifier.shown == [("Content engine", "1 draft to approve", report)]
    assert "1 draft to approve" in result.output


def test_digest_stays_quiet_when_nothing_waits(monkeypatch, tmp_path):
    notifier = digest_world(monkeypatch, tmp_path, drafted=False)
    result = runner.invoke(app, ["digest", "--date", "2026-11-20"])
    assert result.exit_code == 0 and notifier.shown == []
    assert "nothing waits on you" in result.output


def test_digest_no_notify_writes_without_a_toast(monkeypatch, tmp_path):
    notifier = digest_world(monkeypatch, tmp_path)
    result = runner.invoke(app, ["digest", "--date", "2026-11-20", "--no-notify"])
    assert result.exit_code == 0 and notifier.shown == []


def fetch_recorder(monkeypatch, tmp_path) -> list[str]:
    from engine.library import models

    fetched: list[str] = []

    def fake_fetch(self, cache_dir=None):
        fetched.append(self.key)
        return [tmp_path / self.key]

    monkeypatch.setattr(models.PinnedModel, "fetch", fake_fetch)
    return fetched


def test_models_fetch_downloads_the_named_pinned_models(monkeypatch, tmp_path):
    from engine.library.models import PINNED

    fetched = fetch_recorder(monkeypatch, tmp_path)
    result = runner.invoke(app, ["models", "fetch", "clip", "yunet"])
    assert result.exit_code == 0 and fetched == ["clip", "yunet"]
    assert PINNED["clip"].revision[:7] in result.output


def test_models_fetch_without_names_fetches_every_pinned_model(monkeypatch, tmp_path):
    fetched = fetch_recorder(monkeypatch, tmp_path)
    assert runner.invoke(app, ["models", "fetch"]).exit_code == 0
    assert fetched == ["bge", "clip", "yunet"]


def test_models_fetch_refuses_an_unknown_name(monkeypatch, tmp_path):
    fetched = fetch_recorder(monkeypatch, tmp_path)
    result = runner.invoke(app, ["models", "fetch", "clip", "nope"])
    assert result.exit_code == 1 and fetched == [] and "bge, clip, yunet" in result.output


@pytest.fixture
def cli_db(monkeypatch, tmp_path):
    from engine import cli
    from engine.db import open_db

    monkeypatch.setattr(cli, "open_db", lambda path: open_db(tmp_path / "engine.db"))
    return lambda: open_db(tmp_path / "engine.db")


def test_kill_warn_and_resume(cli_db):
    killed = runner.invoke(app, ["kill", "selfhelp", "--reason", "taking a week off"])
    assert killed.exit_code == 0, killed.output
    assert "selfhelp paused" in killed.output and "ce resume selfhelp" in killed.output
    row = cli_db().execute("SELECT kill_flag, kill_reason FROM identities").fetchone()
    assert tuple(row) == (1, "taking a week off")
    resumed = runner.invoke(app, ["resume", "selfhelp"])
    assert resumed.exit_code == 0 and "selfhelp resumed at L0" in resumed.output
    warned = runner.invoke(app, ["warn", "selfhelp", "YouTube strike on post 4"])
    assert warned.exit_code == 0 and "platform warning" in warned.output


def test_kill_all_and_bad_input(cli_db):
    assert runner.invoke(app, ["kill", "--all"]).exit_code == 0
    assert cli_db().execute("SELECT MIN(kill_flag) FROM identities").fetchone()[0] == 1
    unknown = runner.invoke(app, ["kill", "nobody"])
    assert unknown.exit_code == 1 and "nobody" in unknown.output
    assert runner.invoke(app, ["kill"]).exit_code == 1  # an identity or --all
    assert runner.invoke(app, ["resume", "nobody"]).exit_code == 1


def test_index_films_lists_the_connected_volumes(monkeypatch):
    from pathlib import Path

    from engine import cli
    from engine.library.volumes import Volume

    monkeypatch.setattr(cli, "mounted_volumes", lambda: [Volume(Path("E:/"), "1A2B-3C4D", "FILMS")])
    result = runner.invoke(app, ["index-films", "--volumes"])
    assert result.exit_code == 0 and "1A2B-3C4D" in result.output and "FILMS" in result.output


def test_index_films_without_a_film_drive_says_how_to_add_one(cli_db, monkeypatch):
    from engine import cli

    monkeypatch.setattr(cli, "mounted_volumes", lambda: [])
    result = runner.invoke(app, ["index-films"])
    assert result.exit_code == 0 and "--volumes" in result.output


def test_index_films_runs_the_indexer_until_the_deadline(cli_db, monkeypatch, repo_root):
    from dataclasses import replace

    from engine import cli
    from engine.config.models import SourceConfig
    from engine.library.film_index import FilmResult, IndexReport

    films = SourceConfig(id="films", kind="drive", rights_type="film_reference",
                         rights_proof="own collection", volume_serial="1A2B-3C4D",
                         volume_label="FILMS")  # fmt: skip
    config = replace(cli.load_config(), sources=[films])
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "mounted_volumes", lambda: [])
    monkeypatch.setattr(cli, "model_files", lambda: {})
    monkeypatch.setattr(cli, "clip_if_cached", lambda: None)
    seen = {}

    class FakeIndexer:
        def __init__(self, conn, config, deps, media_root):
            seen["deps"] = deps

        def run(self, volumes, until=None, retry_failed=False):
            seen.update(until=until, retry=retry_failed)
            return IndexReport(films=[FilmResult("Heat (1995)", 1, ["probe", "transcript"]),
                                      FilmResult("broken", 2, error="probe: no video")],
                               stopped="deadline 06:00 reached")  # fmt: skip

    monkeypatch.setattr(cli, "FilmIndexer", FakeIndexer)
    from datetime import datetime, timedelta

    until = (datetime.now() + timedelta(hours=1)).strftime("%H:%M")  # inside the 8 h window
    result = runner.invoke(app, ["index-films", "--until", until, "--retry-failed"])
    assert result.exit_code == 0, result.output
    assert seen["retry"] is True and seen["until"].strftime("%H:%M") == until
    assert "Heat (1995): probe, transcript" in result.output
    assert "broken: FAILED probe: no video" in result.output
    assert "Stopped: deadline 06:00 reached" in result.output
    assert seen["deps"].clip is None  # the CLIP model is not in this test's cache
    assert "Keyframe vectors wait for the CLIP model" in result.output


def own_source_config(cli, tmp_path):
    from dataclasses import replace

    from engine.config.models import SourceConfig

    own = SourceConfig(id="own-clips", kind="folder", path=str(tmp_path / "clips"),
                       rights_type="own", rights_proof="Drawn for the tests")  # fmt: skip
    return replace(cli.load_config(), sources=[own])


def test_ingest_without_folder_sources_points_to_g09c(cli_db, monkeypatch):
    from dataclasses import replace

    from engine import cli

    config = replace(cli.load_config(), sources=[])
    monkeypatch.setattr(cli, "load_config", lambda: config)
    result = runner.invoke(app, ["ingest"])
    assert result.exit_code == 0 and "G0.9c" in result.output


def test_ingest_needs_the_models(cli_db, monkeypatch, tmp_path):
    from engine import cli

    config = own_source_config(cli, tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "missing_models", lambda keys: ["clip"])
    result = runner.invoke(app, ["ingest"])
    assert result.exit_code == 1 and "ce models fetch clip" in result.output


def test_ingest_runs_the_ingester(cli_db, monkeypatch, tmp_path):
    from engine import cli
    from engine.library.ingest import AssetResult, IngestReport

    config = own_source_config(cli, tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "missing_models", lambda keys: [])
    monkeypatch.setattr(cli, "real_ingest_models", lambda: ("text", "clip", "faces"))
    seen = {}

    class FakeIngester:
        def __init__(self, conn, config, deps, media_root):
            seen["deps"] = deps

        def run(self, source_id=None, retry_failed=False):
            seen.update(source=source_id, retry=retry_failed)
            return IngestReport(
                assets=[AssetResult("C:/clips/a.mp4", 1, ["probe", "scenes"]),
                        AssetResult("C:/clips/b.mp4", 2, error="probe: no video"),
                        AssetResult("C:/clips/c.mp4", 3)],
                missing=["stock-clips"],
            )  # fmt: skip

    monkeypatch.setattr(cli, "Ingester", FakeIngester)
    result = runner.invoke(app, ["ingest", "--source", "own-clips", "--retry-failed"])
    assert result.exit_code == 0, result.output
    assert seen["source"] == "own-clips" and seen["retry"] is True
    assert seen["deps"].clip == "clip" and seen["deps"].faces == "faces"
    assert "a.mp4: probe, scenes" in result.output
    assert "b.mp4: FAILED probe: no video" in result.output
    assert "c.mp4: already indexed" in result.output
    assert "Folder not found: stock-clips" in result.output
    assert "1 new or changed, 1 failed, 1 unchanged" in result.output


def test_ingest_refuses_an_unknown_source(cli_db, monkeypatch, tmp_path):
    from engine import cli

    config = own_source_config(cli, tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "missing_models", lambda keys: [])
    monkeypatch.setattr(cli, "real_ingest_models", lambda: ("text", "clip", "faces"))
    result = runner.invoke(app, ["ingest", "--source", "nope"])
    assert result.exit_code == 1 and "own-clips" in result.output


def seed_face_clip(conn) -> int:
    conn.execute(
        "INSERT INTO sources (id, kind, path, rights_type, rights_proof) "
        "VALUES ('own-clips', 'folder', 'C:/clips', 'own', 'Drawn for the tests')"
    )
    asset_id = conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, duration, rights_type) "
        "VALUES ('own-clips', ?, 'C:/clips/friend.mp4', 'video', 4.0, 'own')",
        ("f" * 64,),
    ).lastrowid
    conn.execute(
        "INSERT INTO asset_index (asset_id, people_at, faces, face_first_s) "
        "VALUES (?, '2026-11-13T00:00:00Z', 2, 2.0)",
        (asset_id,),
    )
    return asset_id


def test_status_people_lists_the_clips_to_mark(cli_db):
    asset_id = seed_face_clip(cli_db())
    result = runner.invoke(app, ["status", "--people"])
    assert result.exit_code == 0, result.output
    assert f"#{asset_id}" in result.output and "friend.mp4" in result.output
    assert "faces in 2 frames from 2.0 s" in result.output
    assert f"ce people mark {asset_id} yes" in result.output


def test_status_without_people_says_how_many_wait(cli_db):
    seed_face_clip(cli_db())
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0 and "1 clip waits for a people mark" in result.output


def test_status_people_when_nothing_waits(cli_db):
    result = runner.invoke(app, ["status", "--people"])
    assert result.exit_code == 0 and "No clips wait for a people mark" in result.output


def test_people_mark_sets_the_release(cli_db):
    asset_id = seed_face_clip(cli_db())
    result = runner.invoke(app, ["people", "mark", str(asset_id), "yes", "--note", "consent"])
    assert result.exit_code == 0, result.output
    assert "friend.mp4" in result.output and "yes" in result.output
    row = cli_db().execute("SELECT people_release FROM assets WHERE id = ?", (asset_id,))
    assert row.fetchone()[0] == "yes"
    assert runner.invoke(app, ["people", "mark", "999", "yes"]).exit_code == 1
    assert runner.invoke(app, ["people", "mark", str(asset_id), "maybe"]).exit_code == 1


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_ingest_imports_the_music_pool_without_the_models(cli_db, monkeypatch, tmp_path):
    """FR-ING-6: music needs no embedding model, so it imports before the footage check."""
    from dataclasses import replace

    from library_fixtures import make_track

    from engine import cli
    from engine.config.models import SourceConfig

    (tmp_path / "music").mkdir()
    make_track(tmp_path / "music" / "calm.mp3", 80, seconds=8, title="Calm", artist="Fixture")
    pool = SourceConfig(id="music-pool", kind="library", path=str(tmp_path / "music"),
                        rights_type="stock", rights_proof="Audio library licence",
                        licence_url="https://licence.example/audio", attribution="")  # fmt: skip
    config = replace(own_source_config(cli, tmp_path), sources=[pool])
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "missing_models", lambda keys: ["clip"])
    result = runner.invoke(app, ["ingest", "--source", "music-pool"])
    assert result.exit_code == 0, result.output
    assert "calm.mp3: imported" in result.output and "1 track imported" in result.output
    assert "ce models fetch" not in result.output
