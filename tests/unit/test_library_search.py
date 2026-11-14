"""`search_library` over the ingested library (SRD §5.4, FR-UND-3, §8.1, §8.3): rights-clean,
people-clean, licence-valid cutaways ranked by meaning. P0.6b.1's done-when is the first
test: five sample beats on fixture footage. Models are the fakes; the real ones run in
`test_library_beats_model.py`."""

from pathlib import Path

import pytest
from library_fixtures import SAMPLE_BEATS, SPOKEN, TODAY, SpokenWhisper, build_library

from engine.agents.context import ToolContext
from engine.agents.state import sync_identities
from engine.agents.tools import call_tool
from engine.library.clip import FakeClip
from engine.library.embeddings import FakeEmbedder
from engine.library.faces import FakeFaces
from engine.library.ingest import IngestDeps
from engine.library.people import mark
from engine.library.search import SearchModels

pytestmark = pytest.mark.media  # draws or probes clips with ffmpeg (ADR 0024)

UNCLEAN = {"friend", "old-forest", "Sea Film (1999)"}  # a face, an expired licence, a film


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    folder = tmp_path_factory.mktemp("library")
    deps = IngestDeps(embedder=FakeEmbedder(), clip=FakeClip(), faces=FakeFaces(),
                      transcriber=SpokenWhisper)  # fmt: skip
    ingester, ids = build_library(folder, deps, folder / "media")
    sync_identities(ingester.conn, ingester.config)
    return ingester, ids


def make_ctx(library, models=True) -> ToolContext:
    ingester, _ = library
    search = SearchModels(text=FakeEmbedder(), clip=FakeClip()) if models else None
    return ToolContext(conn=ingester.conn, config=ingester.config, today=TODAY,
                       search_models=search)  # fmt: skip


def names(library, rows) -> list[str]:
    by_id = {v: k for k, v in library[1].items()}
    return [by_id[r["asset_id"]] for r in rows]


def search(ctx, query, fmt="C", limit=10) -> dict:
    result = call_tool(ctx, "search_library", {"query": query, "format": fmt, "limit": limit})
    assert result["ok"], result
    return result


def test_five_sample_beats_return_clean_cutaways(library):
    """The done-when: rights-clean, people-clean cutaway candidates for 5 sample beats."""
    ctx = make_ctx(library)
    conn = library[0].conn
    for kind, beat in SAMPLE_BEATS:
        results = search(ctx, beat)["results"]
        found = names(library, results)
        assert found[0] == kind, f"{beat!r}: {found}"
        assert not UNCLEAN & set(found), f"{beat!r}: {found}"
        for row in results:
            asset = conn.execute(
                "SELECT rights_type, people_release FROM assets WHERE id = ?", (row["asset_id"],)
            ).fetchone()
            assert asset["rights_type"] in ("own", "stock", "ai_generated")
            assert asset["people_release"] in ("none", "yes")
            assert row["match"] == "image" and Path(row["keyframe_path"]).is_file()
            assert 0 <= row["start_s"] < row["end_s"]
        assert len(found) == len(set(found))  # the best scene of each clip, once


def test_format_a_may_use_the_film(library):
    found = names(library, search(make_ctx(library), "waves rolling on a blue ocean", "A")[
        "results"])  # fmt: skip
    assert "Sea Film (1999)" in found
    assert "friend" not in found and "old-forest" not in found


def test_spoken_matches_come_from_transcripts(library):
    spoken = search(make_ctx(library), SPOKEN)["spoken"]
    assert names(library, spoken)[0] == "ocean"
    assert spoken[0]["match"] == "transcript" and spoken[0]["text"] == SPOKEN


def test_a_released_face_clip_becomes_searchable(library):
    ingester, ids = library
    ctx = make_ctx(library)
    beat = SAMPLE_BEATS[0][1]
    assert "friend" not in names(library, search(ctx, beat)["results"])
    mark(ingester.conn, ids["friend"], "yes", "consent held")
    try:
        assert "friend" in names(library, search(ctx, beat)["results"])
    finally:  # leave the module's library as it was
        ingester.conn.execute("DELETE FROM people_marks")
        ingester.conn.execute(
            "UPDATE assets SET people_release = 'unknown' WHERE id = ?", (ids["friend"],)
        )


def test_without_the_models_it_falls_back_to_text_and_says_so(library):
    result = search(make_ctx(library, models=False), "ocean")
    assert "text match" in result["note"]
    assert names(library, result["results"]) == ["ocean"]  # by file name


def test_a_query_with_nothing_embedded_yet_finds_nothing(tmp_path, library):
    from engine.db import open_db

    ingester, _ = library
    conn = open_db(tmp_path / "empty.db")
    sync_identities(conn, ingester.config)
    ctx = ToolContext(
        conn=conn,
        config=ingester.config,
        today=TODAY,
        search_models=SearchModels(text=FakeEmbedder(), clip=FakeClip()),
    )
    result = search(ctx, SAMPLE_BEATS[0][1])
    assert result["results"] == [] and result["spoken"] == []


def test_an_empty_query_lists_the_eligible_clips(library):
    found = set(names(library, search(make_ctx(library), "")["results"]))
    assert found == {"sunrise", "ocean", "night", "forest", "notebook"}
