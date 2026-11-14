"""The music pool (SRD FR-ING-6, FR-SRC-2, §8.1; ADR 0023): tracks enter only from library
sources in the sources config, each with its licence terms, and come back from
`search_library` for a bed. P0.6b.2's done-when is the first test. Tracks are generated
click tracks (`library_fixtures.make_track`), never real songs (NFR-9)."""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import date

import pytest
from library_fixtures import make_track

from engine.agents.context import ToolContext
from engine.agents.state import sync_identities
from engine.agents.tools import call_tool
from engine.config import load_config
from engine.config.models import SourceConfig
from engine.db import open_db
from engine.library.assets import register_generated
from engine.library.music import MusicImporter, music_sources
from engine.produce.voice import silent_wav_bytes
from engine.style.look import energy_band

pytestmark = pytest.mark.media  # draws or probes clips with ffmpeg (ADR 0024)

TODAY = date(2026, 11, 20)
TRACKS = {  # file: (bpm, title, artist)
    "Slow Morning.mp3": (70, "Slow Morning", "Fixture Band"),
    "steady.m4a": (100, "Steady Steps", "Fixture Band"),
    "untagged-drive.wav": (140, None, None),
}


@pytest.fixture(scope="module")
def tracks(tmp_path_factory):
    folder = tmp_path_factory.mktemp("music-fixtures")
    for name, (bpm, title, artist) in TRACKS.items():
        make_track(folder / name, bpm, title=title, artist=artist)
    return folder


def pool(folder, **fields) -> SourceConfig:
    return SourceConfig(**{
        "id": "music-pool", "kind": "library", "path": str(folder), "rights_type": "stock",
        "rights_proof": "Audio library licence: free in videos, no attribution",
        "licence_url": "https://licence.example/audio", "attribution": "", **fields,
    })  # fmt: skip


@pytest.fixture
def env(tmp_path, repo_root, tracks):
    folder = tmp_path / "music"
    shutil.copytree(tracks, folder)
    config = replace(load_config(repo_root / "config"), sources=[pool(folder)])
    conn = open_db(tmp_path / "engine.db")
    sync_identities(conn, config)
    ctx = ToolContext(conn=conn, config=config, config_dir=repo_root / "config", today=TODAY,
                      media_root=tmp_path / "media")  # fmt: skip
    return ctx, folder


def run(ctx, **kw):
    return MusicImporter(ctx.conn, ctx.config).run(**kw)


def music(ctx, query="", fmt="C", **kw) -> list[dict]:
    result = call_tool(ctx, "search_library", {"kind": "music", "query": query, "format": fmt,
                                               **kw})  # fmt: skip
    assert result["ok"], result
    return result["results"]


def titles(rows) -> list[str]:
    return [r["title"] for r in rows]


def test_imported_tracks_come_back_from_search_library(env):
    """The done-when: imported tracks come back from `search_library`."""
    ctx, _ = env
    report = run(ctx)
    assert [t.error for t in report.tracks] == [None, None, None]
    found = music(ctx)
    assert sorted(titles(found)) == ["Slow Morning", "Steady Steps", "untagged-drive"]
    for row in found:
        assert row["match"] == "music" and row["rights_type"] == "stock"
        assert row["licence_url"] == "https://licence.example/audio"
        assert row["duration"] == pytest.approx(20.0, abs=0.1)
    slow = next(r for r in found if r["title"] == "Slow Morning")
    assert slow["artist"] == "Fixture Band"


def test_tempo_and_energy_are_measured(env):
    ctx, _ = env
    run(ctx)
    rows = {r["title"]: r for r in music(ctx)}
    for title, bpm in (("Slow Morning", 70), ("Steady Steps", 100), ("untagged-drive", 140)):
        assert rows[title]["tempo_bpm"] == pytest.approx(bpm, abs=3), title
        assert rows[title]["energy"] == energy_band(bpm)
    assert [energy_band(b) for b in (70, 100, 140)] == ["low", "medium", "high"]


def test_the_look_feel_orders_the_tracks(env):
    ctx, _ = env
    run(ctx)
    assert titles(music(ctx, energy="low", tempo_bpm=[60, 85]))[0] == "Slow Morning"
    assert titles(music(ctx, energy="high", tempo_bpm=[125, 155]))[0] == "untagged-drive"
    assert titles(music(ctx, tempo_bpm=[95, 105]))[0] == "Steady Steps"
    assert titles(music(ctx, "steady"))[0] == "Steady Steps"


def test_each_track_has_a_licence_record_with_the_source_terms(env):
    ctx, folder = env
    run(ctx)
    rows = ctx.conn.execute(
        "SELECT l.* FROM asset_licences l JOIN tracks t ON t.asset_id = l.asset_id"
    ).fetchall()
    assert len(rows) == 3
    for row in rows:
        assert row["licence_url"] == "https://licence.example/audio"
        assert row["terms"] == "Audio library licence: free in videos, no attribution"
        assert row["attribution"] == ""
    renewed = pool(folder, licence_url="https://licence.example/v2", attribution="Music: pool")
    ctx.config = replace(ctx.config, sources=[renewed])
    run(ctx)
    urls = {r[0] for r in ctx.conn.execute("SELECT licence_url FROM asset_licences")}
    assert urls == {"https://licence.example/v2"}
    assert {r["attribution"] for r in music(ctx)} == {"Music: pool"}


def test_scanning_twice_adds_no_rows(env):
    ctx, _ = env
    run(ctx)
    counts = [ctx.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("assets", "tracks", "asset_licences", "asset_paths")]  # fmt: skip
    again = run(ctx)
    assert not any(t.imported for t in again.tracks)
    assert counts == [ctx.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                      for t in ("assets", "tracks", "asset_licences", "asset_paths")]  # fmt: skip


def test_an_expired_source_hides_its_tracks(env):
    ctx, folder = env
    run(ctx)
    ctx.config = replace(ctx.config, sources=[pool(folder, expires_at=date(2026, 1, 1))])
    run(ctx)
    assert music(ctx) == []


def test_a_track_without_its_licence_record_is_not_returned(env):
    ctx, _ = env
    run(ctx)
    gone = music(ctx)[0]["asset_id"]
    ctx.conn.execute("DELETE FROM asset_licences WHERE asset_id = ?", (gone,))
    assert gone not in {r["asset_id"] for r in music(ctx)}


def test_voice_wavs_are_not_music(env, tmp_path):
    ctx, _ = env
    wav = tmp_path / "voice.wav"
    wav.write_bytes(silent_wav_bytes(2.0))
    register_generated(ctx.conn, wav, "audio", 2.0)
    run(ctx)
    assert "voice" not in titles(music(ctx))
    assert len(music(ctx)) == 3


def test_music_never_shows_up_as_footage(env):
    ctx, _ = env
    run(ctx)
    footage = call_tool(ctx, "search_library", {"query": "", "format": "C"})["results"]
    assert footage == []


def test_a_broken_track_is_recorded_and_skipped(env):
    ctx, folder = env
    (folder / "broken.mp3").write_bytes(b"ID3 not really audio")
    first = {t.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: t for t in run(ctx).tracks}
    assert first["broken.mp3"].error and len(music(ctx)) == 3
    second = {t.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: t for t in run(ctx).tracks}
    assert second["broken.mp3"].skipped and not second["broken.mp3"].error
    retried = {t.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: t
               for t in run(ctx, retry_failed=True).tracks}  # fmt: skip
    assert retried["broken.mp3"].error


def test_music_comes_only_from_library_pools(env, repo_root):
    config = load_config(repo_root / "config")
    assert music_sources(config) == []  # off until Abbas enables the pool (ADR 0023)
    ctx, _ = env
    report = MusicImporter(ctx.conn, ctx.config).run(source_id="music-pool")
    assert len(report.tracks) == 3
    with pytest.raises(ValueError, match="music-pool"):
        MusicImporter(ctx.conn, ctx.config).run(source_id="pexels")


def test_a_missing_pool_folder_is_reported(env, tmp_path):
    ctx, _ = env
    ctx.config = replace(ctx.config, sources=[pool(tmp_path / "nowhere")])
    assert run(ctx).missing == ["music-pool"]
