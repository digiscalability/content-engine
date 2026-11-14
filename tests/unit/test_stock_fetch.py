"""Stock fetched only for beats a scriptwriter asked for, capped per day, each clip with its
licence record (SRD FR-ING-5, FR-SRC-2, NFR-5, GR-7; ADR 0023). The provider is FakeStock;
fixture clips are drawn by `library_fixtures.py`."""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest
from library_fixtures import SpokenWhisper, make_clip
from typer.testing import CliRunner

from engine import cli
from engine.agents.context import ToolContext
from engine.agents.state import sync_identities
from engine.agents.tools import call_tool
from engine.config import load_config
from engine.config.models import SourceConfig
from engine.db import open_db
from engine.library.clip import FakeClip
from engine.library.embeddings import FakeEmbedder
from engine.library.faces import FakeFaces
from engine.library.ingest import IngestDeps, Ingester
from engine.library.search import SearchModels
from engine.library.stock import (
    FakeStock,
    FakeStockEntry,
    StockError,
    StockFile,
    StockRateLimited,
    StockVideo,
)
from engine.library.stock_fetch import fetch_requested, stock_folder
from engine.library.vectors import store_image

TODAY = date(2026, 11, 20)
OCEAN = "waves rolling on a blue ocean"
FOREST = "a green forest of pine trees"
PEXELS = SourceConfig(
    id="pexels",
    kind="library",
    url="https://api.pexels.com/v1/videos/",
    rights_type="stock",
    rights_proof="Pexels licence",
    licence_url="https://www.pexels.com/license/",
    attribution="Pexels",
)
OWN = SourceConfig(
    id="own-clips",
    kind="folder",
    path="C:/nowhere",
    rights_type="own",
    rights_proof="Drawn for the tests",
)


def video(vid: str, creator: str = "Ana Lens", duration: float = 12, size=(1080, 1920)):
    files = (
        StockFile(
            id=f"f{vid}",
            width=size[0],
            height=size[1],
            file_type="video/mp4",
            link=f"https://videos.pexels.com/video-files/{vid}/hd.mp4",
        ),
    )
    return StockVideo(
        id=vid,
        page_url=f"https://www.pexels.com/video/clip-{vid}/",
        duration=duration,
        width=size[0],
        height=size[1],
        creator=creator,
        creator_url=f"https://www.pexels.com/@{vid}",
        files=files,
    )


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> dict[str, Path]:
    folder = tmp_path_factory.mktemp("stock-fixtures")
    return {kind: make_clip(folder / f"{kind}.mp4", [kind]) for kind in ("ocean", "forest")}


def catalogue(clips, *entries) -> FakeStock:
    return FakeStock(
        [FakeStockEntry(v, frozenset(words), clips[kind]) for v, words, kind in entries]
    )


def ocean_stock(clips) -> FakeStock:
    return catalogue(clips, (video("101"), {"ocean", "waves"}, "ocean"))


@pytest.fixture
def env(tmp_path, repo_root):
    config = replace(load_config(repo_root / "config"), sources=[PEXELS, OWN])
    conn = open_db(tmp_path / "engine.db")
    sync_identities(conn, config)
    ctx = ToolContext(
        conn=conn,
        config=config,
        config_dir=repo_root / "config",
        today=TODAY,
        media_root=tmp_path / "media",
        search_models=SearchModels(text=FakeEmbedder(), clip=FakeClip()),
    )
    return ctx


def with_stock(ctx, **fields) -> ToolContext:
    engine = ctx.config.engine
    engine = engine.model_copy(update={"stock": engine.stock.model_copy(update=fields)})
    ctx.config = replace(ctx.config, engine=engine)
    return ctx


def brief(ctx) -> int:
    return ctx.conn.execute(
        "INSERT INTO briefs (identity_id, format, topic, angle, hook, cta, difference_note) "
        "VALUES ('selfhelp', 'C', 'focus', 'craft', 'One habit', 'follow', 'new')"
    ).lastrowid


def ask(ctx, query: str, brief_id: int | None) -> dict:
    args = {"query": query, "format": "C", **({"brief_id": brief_id} if brief_id else {})}
    result = call_tool(ctx, "search_library", args)
    assert result["ok"], result
    return result


def requests(ctx) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute("SELECT * FROM stock_requests ORDER BY id")]


def fetch(ctx, provider, today=TODAY, **kw):
    return fetch_requested(ctx.conn, ctx.config, provider, ctx.media_root, today, **kw)


def ingest(ctx):
    deps = IngestDeps(
        embedder=FakeEmbedder(), clip=FakeClip(), faces=FakeFaces(), transcriber=SpokenWhisper
    )
    return Ingester(ctx.conn, ctx.config, deps, ctx.media_root).run()


def own_clip_showing(ctx, words: str) -> int:
    """An own clip whose one keyframe matches `words` exactly under the fake CLIP."""
    ctx.conn.execute(
        "INSERT OR IGNORE INTO sources (id, kind, path, rights_type, rights_proof) "
        "VALUES ('own-clips', 'folder', 'C:/nowhere', 'own', 'drawn')"
    )
    asset = ctx.conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, rights_type, people_release) "
        "VALUES ('own-clips', ?, 'C:/nowhere/a.mp4', 'video', 'own', 'none')",
        ("e" * 64,),
    ).lastrowid
    scene = ctx.conn.execute(
        "INSERT INTO scenes (asset_id, start_s, end_s, keyframe_path) VALUES (?, 0, 2, 'k.jpg')",
        (asset,),
    ).lastrowid
    store_image(ctx.conn, scene, FakeClip().embed_texts([words])[0], FakeClip.name)
    return asset


# Queueing: only the scriptwriter's weak beat queries ---------------------------------------


def test_a_weak_beat_query_of_a_brief_is_queued(env):
    b = brief(env)
    result = ask(env, OCEAN, b)
    (row,) = requests(env)
    assert result["stock_request"] == row["id"]
    assert (row["brief_id"], row["query"], row["format"], row["status"]) == (
        b,
        OCEAN,
        "C",
        "pending",
    )


def test_a_strong_match_is_not_queued(env):
    own_clip_showing(env, OCEAN)
    result = ask(env, OCEAN, brief(env))
    assert result["results"][0]["score"] >= env.config.engine.stock.thin_below
    assert result["stock_request"] is None and requests(env) == []


def test_a_weak_match_is_queued_with_its_score(env):
    own_clip_showing(env, FOREST)
    ask(env, OCEAN, brief(env))
    (row,) = requests(env)
    assert row["best_score"] is not None and row["best_score"] < env.config.engine.stock.thin_below


def test_no_brief_no_request(env):
    assert "stock_request" not in ask(env, OCEAN, None)
    assert requests(env) == []


def test_the_same_beat_twice_is_one_request(env):
    b = brief(env)
    first = ask(env, OCEAN, b)["stock_request"]
    assert ask(env, f"  {OCEAN} ", b)["stock_request"] == first
    assert len(requests(env)) == 1


def test_without_a_stock_source_nothing_is_queued(env):
    env.config = replace(env.config, sources=[OWN])
    assert ask(env, OCEAN, brief(env))["stock_request"] is None


def test_an_unknown_brief_is_refused(env):
    result = call_tool(env, "search_library", {"query": OCEAN, "format": "C", "brief_id": 99})
    assert not result["ok"] and "no brief 99" in result["errors"][0]


# Fetching ------------------------------------------------------------------------------------


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_fetched_clips_carry_licence_records_and_become_searchable(env, clips):
    """FR-ING-5's accept line: a fetched clip without a licence record is not searchable."""
    ask(env, OCEAN, brief(env))
    report = fetch(env, ocean_stock(clips))
    (asset,) = report.fetched
    licence = dict(
        env.conn.execute("SELECT * FROM asset_licences WHERE asset_id = ?", (asset,)).fetchone()
    )
    assert licence["licence_url"] == "https://www.pexels.com/license/"
    assert licence["terms"] == "Pexels licence"
    assert licence["attribution"] == "Video by Ana Lens on Pexels"
    assert (licence["creator"], licence["creator_url"]) == (
        "Ana Lens",
        "https://www.pexels.com/@101",
    )
    assert licence["page_url"] == "https://www.pexels.com/video/clip-101/"
    assert (licence["provider"], licence["provider_id"]) == ("fake-pexels", "101")
    assert licence["fetched_on"] == TODAY.isoformat()
    path = Path(env.conn.execute("SELECT path FROM assets WHERE id = ?", (asset,)).fetchone()[0])
    assert path.parent == stock_folder(env.media_root, "pexels") and path.is_file()
    assert requests(env)[0]["status"] == "fetched"

    ingest(env)
    found = ask(env, OCEAN, None)["results"]
    assert found[0]["asset_id"] == asset and found[0]["match"] == "image"
    env.conn.execute("DELETE FROM asset_licences WHERE asset_id = ?", (asset,))
    assert asset not in {r["asset_id"] for r in ask(env, OCEAN, None)["results"]}


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_the_daily_cap_holds_across_runs(env, clips):
    with_stock(env, daily_cap=1, per_request=2)
    stock = catalogue(
        clips,
        (video("101"), {"ocean"}, "ocean"),
        (video("102"), {"ocean"}, "ocean"),
        (video("201"), {"forest"}, "forest"),
    )
    b = brief(env)
    ask(env, OCEAN, b)
    ask(env, FOREST, b)
    first = fetch(env, stock)
    assert len(first.fetched) == 1 and "cap" in first.stopped
    assert [r["status"] for r in requests(env)] == ["fetched", "pending"]
    again = fetch(env, stock)
    assert again.fetched == [] and stock.searches == [OCEAN]  # nothing searched once spent
    tomorrow = fetch(env, stock, today=TODAY + timedelta(days=1))
    assert len(tomorrow.fetched) == 1 and requests(env)[1]["status"] == "fetched"


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_only_full_hd_portrait_clips_within_the_length_are_taken(env, clips):
    stock = catalogue(
        clips,
        (video("1", size=(1920, 1080)), {"ocean"}, "ocean"),
        (video("2", duration=45), {"ocean"}, "ocean"),
        (video("3", size=(720, 1280)), {"ocean"}, "ocean"),
        (video("4"), {"ocean"}, "ocean"),
    )
    ask(env, OCEAN, brief(env))
    report = fetch(env, stock)
    assert stock.downloads == ["4"] and len(report.fetched) == 1


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_a_clip_is_never_fetched_twice(env, clips):
    stock = ocean_stock(clips)
    b = brief(env)
    ask(env, OCEAN, b)
    ask(env, "the ocean at dusk", b)
    report = fetch(env, stock)
    assert stock.downloads == ["101"] and len(report.fetched) == 1
    assert [r["status"] for r in requests(env)] == ["fetched", "none_found"]


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_a_killed_identity_gets_no_stock(env, clips):
    ask(env, OCEAN, brief(env))
    env.conn.execute("UPDATE identities SET kill_flag = 1 WHERE id = 'selfhelp'")
    stock = ocean_stock(clips)
    assert fetch(env, stock).fetched == [] and stock.searches == []
    assert requests(env)[0]["status"] == "pending"


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_rate_limit_stops_and_keeps_requests(env, clips):
    class Limited(FakeStock):
        def search(self, query, per_page):
            raise StockRateLimited("Pexels search: rate limit reached (429)")

    b = brief(env)
    ask(env, OCEAN, b)
    ask(env, FOREST, b)
    report = fetch(env, Limited([]))
    assert "429" in report.stopped
    assert [r["status"] for r in requests(env)] == ["pending", "pending"]


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_a_provider_error_fails_that_request_only(env, clips):
    class Flaky(FakeStock):
        def search(self, query, per_page):
            if "forest" in query:
                raise StockError("Pexels search failed (500): oops")
            return super().search(query, per_page)

    stock = Flaky(ocean_stock(clips).catalogue)
    b = brief(env)
    ask(env, FOREST, b)
    ask(env, OCEAN, b)
    report = fetch(env, stock)
    assert len(report.fetched) == 1
    statuses = {r["query"]: (r["status"], r["detail"]) for r in requests(env)}
    assert statuses[FOREST][0] == "failed" and "500" in statuses[FOREST][1]
    assert statuses[OCEAN][0] == "fetched"


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_the_disk_stop_line_stops_the_fetch(env, clips):
    ask(env, OCEAN, brief(env))
    stock = ocean_stock(clips)
    report = fetch(env, stock, disk_free=lambda path: 1 << 30)  # 1 GB free
    assert report.stopped and stock.searches == []
    assert requests(env)[0]["status"] == "pending"


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_a_dry_run_calls_and_writes_nothing(env, clips):
    ask(env, OCEAN, brief(env))
    stock = ocean_stock(clips)
    report = fetch(env, stock, dry_run=True)
    assert [r.query for r in report.requests] == [OCEAN] and report.left_today == 10
    assert stock.searches == [] and report.fetched == []
    assert env.conn.execute("SELECT count(*) FROM asset_licences").fetchone()[0] == 0


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_a_hand_dropped_stock_file_is_not_searchable(env, clips):
    folder = stock_folder(env.media_root, "pexels")
    folder.mkdir(parents=True)
    shutil.copyfile(clips["ocean"], folder / "dropped.mp4")
    ingest(env)
    asset = env.conn.execute(
        "SELECT a.id FROM assets a JOIN asset_index i ON i.asset_id = a.id "
        "WHERE i.images_at IS NOT NULL"
    ).fetchone()[0]
    assert asset not in {r["asset_id"] for r in ask(env, OCEAN, None)["results"]}


# The command and the nightly wrapper ---------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def cli_env(env, monkeypatch):
    monkeypatch.setattr(cli, "_engine_db", lambda: (env.config, env.conn))
    monkeypatch.setattr(cli, "_media_root", lambda config: env.media_root)
    return env


def test_stock_fetch_with_nothing_asked_exits_0(cli_env):
    result = runner.invoke(cli.app, ["stock", "fetch"])
    assert result.exit_code == 0, result.output
    assert "No stock requests" in result.output


def test_stock_fetch_without_a_key_points_at_g0_9a(cli_env, monkeypatch):
    ask(cli_env, OCEAN, brief(cli_env))
    monkeypatch.setattr(cli, "make_stock", lambda: None)
    result = runner.invoke(cli.app, ["stock", "fetch"])
    assert result.exit_code == 2, result.output
    assert "PEXELS_API_KEY" in result.output and "G0.9a" in result.output


def test_stock_fetch_dry_run_lists_the_queue(cli_env, monkeypatch):
    ask(cli_env, OCEAN, brief(cli_env))
    monkeypatch.setattr(cli, "make_stock", lambda: pytest.fail("no provider in a dry run"))
    result = runner.invoke(cli.app, ["stock", "fetch", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert OCEAN in result.output and "10 of 10 clips left today" in result.output


@pytest.mark.media  # runs ffmpeg (ADR 0024)
def test_stock_fetch_fetches_with_the_provider(cli_env, clips, monkeypatch):
    ask(cli_env, OCEAN, brief(cli_env))
    monkeypatch.setattr(cli, "make_stock", lambda: ocean_stock(clips))
    result = runner.invoke(cli.app, ["stock", "fetch"])
    assert result.exit_code == 0, result.output
    assert "1 clip fetched" in result.output


def test_fake_clip_vectors_fit_the_table():
    assert (
        FakeClip().embed_texts([OCEAN]).shape[1] == 512
        and np.isfinite(FakeClip().embed_texts([OCEAN])).all()
    )


def test_stock_is_off_in_the_repo_config_until_abbas_enables_it(repo_root):
    from engine.library.stock_fetch import stock_source

    assert stock_source(load_config(repo_root / "config")) is None
