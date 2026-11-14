"""P0.6b.1's done-when on the real models: the fixture library ingested with bge-small,
CLIP ViT-B/32 and YuNet (Whisper stays a fake), then `search_library` for the five sample
beats. Needs the cached models (`uv run ce models fetch`)."""

import pytest
from library_fixtures import SAMPLE_BEATS, TODAY, SpokenWhisper, build_library

from engine.agents.context import ToolContext
from engine.agents.state import sync_identities
from engine.agents.tools import call_tool
from engine.library.clip import ClipOnnx
from engine.library.embeddings import BgeSmall
from engine.library.faces import YuNet
from engine.library.ingest import IngestDeps
from engine.library.search import SearchModels

pytestmark = pytest.mark.model


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    folder = tmp_path_factory.mktemp("real-library")
    text, clip = BgeSmall(), ClipOnnx()
    deps = IngestDeps(embedder=text, clip=clip, faces=YuNet(), transcriber=SpokenWhisper)
    ingester, ids = build_library(folder, deps, folder / "media")
    sync_identities(ingester.conn, ingester.config)
    ctx = ToolContext(conn=ingester.conn, config=ingester.config, today=TODAY,
                      search_models=SearchModels(text=text, clip=clip))  # fmt: skip
    return ctx, {v: k for k, v in ids.items()}


def test_yunet_sends_the_drawn_person_to_review(library):
    ctx, names = library
    rows = ctx.conn.execute("SELECT id, people_release FROM assets").fetchall()
    releases = {names[r["id"]]: r["people_release"] for r in rows}
    assert releases["friend"] == "unknown"
    assert all(releases[kind] == "none" for kind, _ in SAMPLE_BEATS)


def test_five_sample_beats_on_the_real_models(library):
    ctx, names = library
    for kind, beat in SAMPLE_BEATS:
        result = call_tool(ctx, "search_library", {"query": beat, "format": "C", "limit": 5})
        found = [names[r["asset_id"]] for r in result["results"]]
        assert found[0] == kind, f"{beat!r}: {found}"
        assert not {"friend", "old-forest", "Sea Film (1999)"} & set(found)


def test_spoken_search_on_bge(library):
    ctx, names = library
    result = call_tool(ctx, "search_library",
                       {"query": "slow breathing, calm water", "format": "C"})  # fmt: skip
    assert names[result["spoken"][0]["asset_id"]] == "ocean"


# P0.6b.2: where a weak match starts (stock.thin_below, FR-ING-5, ADR 0023) ------------------

MISSES = [  # beats none of the drawn clips shows
    "a red sports car on a city street",
    "a steaming cup of coffee on a wooden table",
    "a crowd of runners in a city marathon",
    "a dog catching a frisbee in a park",
    "close-up of hands typing on a laptop keyboard",
]


def test_thin_below_separates_matches_from_misses(library):
    """The real CLIP's best score for each sample beat clears `thin_below`, and a beat the
    library cannot show stays under it, so only the misses queue a stock fetch."""
    ctx, _ = library

    def best(query: str) -> float:
        result = call_tool(ctx, "search_library", {"query": query, "format": "C", "limit": 1})
        return result["results"][0]["score"]

    hits = {beat: best(beat) for _, beat in SAMPLE_BEATS}
    misses = {beat: best(beat) for beat in MISSES}
    print(f"\nhits {sorted(hits.values())}\nmisses {sorted(misses.values())}")
    thin = ctx.config.engine.stock.thin_below
    assert min(hits.values()) >= thin, hits
    assert max(misses.values()) < thin, misses
