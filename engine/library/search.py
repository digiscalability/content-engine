"""Library search behind the `search_library` tool (SRD §5.4, FR-UND-3, §8.1, §8.3). ADR 0022.

Eligibility comes first, in SQL, so a forbidden clip can never crowd out or slip into the
results: the format's rights tiers, video only, no unreleased faces (`people_release` none
or yes), licence and source not expired. Then ranking by meaning, with sqlite-vec's
`vec_distance_cosine`:
- `results`: scenes whose keyframe's CLIP vector is nearest the query's CLIP text vector,
  the best scene of each clip once (the cutaway candidates);
- `spoken`: transcript chunks whose text vector is nearest the query's (what was said).
Only vectors from the models in use are compared. Without models, or for an empty query,
it lists eligible clips and says so in `note`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np

from engine.library.clip import ImageTextEmbedder
from engine.library.embeddings import Embedder, to_blob

# FR-ING-5, ADR 0023: an asset from a library source (a stock API, a music pool) counts only
# with its own licence record.
LICENSED = ("(s.kind <> 'library' "
            "OR EXISTS (SELECT 1 FROM asset_licences l WHERE l.asset_id = a.id))")  # fmt: skip
ELIGIBLE = f"""a.rights_type IN ({{tiers}}) AND a.media_kind = 'video'
      AND a.people_release IN ('none', 'yes')
      AND (a.licence_expires_at IS NULL OR a.licence_expires_at >= :today)
      AND (s.expires_at IS NULL OR s.expires_at >= :today)
      AND {LICENSED}"""


@dataclass(frozen=True)
class SearchModels:
    text: Embedder | None = None  # bge-small, for transcript chunks
    clip: ImageTextEmbedder | None = None  # CLIP, for keyframes


def _filters(tiers: Sequence[str], today: date) -> tuple[str, dict]:
    names = [f":t{n}" for n in range(len(tiers))]
    params = {"today": today.isoformat(), **{f"t{n}": t for n, t in enumerate(tiers)}}
    return ELIGIBLE.format(tiers=", ".join(names)), params


def _listing(conn: sqlite3.Connection, where: str, params: dict, query: str,
             limit: int) -> list[dict]:  # fmt: skip
    """Eligible clips with their scored moments; a query matches moment text or file path."""
    rows = conn.execute(
        f"""SELECT a.id AS asset_id, a.source_id, a.rights_type, a.duration, a.media_kind,
                   m.id AS moment_id, m.start_s, m.end_s, m.text, m.hook_score
            FROM assets a
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN moments m ON m.asset_id = a.id
            WHERE {where}
              AND (:query = '' OR m.text LIKE :like OR a.path LIKE :like)
            ORDER BY m.hook_score DESC NULLS LAST, a.id
            LIMIT :limit""",
        {**params, "query": query, "like": f"%{query}%", "limit": limit},
    )
    return [{**dict(r), "match": "none"} for r in rows]


def _by_image(conn: sqlite3.Connection, where: str, params: dict, query: str, limit: int,
              clip: ImageTextEmbedder) -> list[dict]:  # fmt: skip
    vector = clip.embed_texts([query])[0]
    if not np.any(vector):
        return []
    rows = conn.execute(
        f"""SELECT a.id AS asset_id, a.source_id, a.rights_type, a.duration, a.media_kind,
                   sc.id AS scene_id, sc.start_s, sc.end_s, sc.keyframe_path,
                   1 - vec_distance_cosine(v.embedding, :vector) AS score
            FROM scenes sc
            JOIN image_vec v ON v.rowid = sc.id
            JOIN assets a ON a.id = sc.asset_id
            JOIN sources s ON s.id = a.source_id
            WHERE {where} AND sc.image_model = :model
            ORDER BY score DESC""",
        {**params, "vector": to_blob(vector), "model": clip.name},
    )
    best: dict[int, dict] = {}
    for row in rows:
        if row["asset_id"] not in best:
            best[row["asset_id"]] = {**dict(row), "score": round(row["score"], 4),
                                     "match": "image"}  # fmt: skip
            if len(best) == limit:
                break
    return list(best.values())


def _by_transcript(conn: sqlite3.Connection, where: str, params: dict, query: str,
                   limit: int, text: Embedder) -> list[dict]:  # fmt: skip
    vector = text.embed([query])[0]
    rows = conn.execute(
        f"""SELECT a.id AS asset_id, a.source_id, a.rights_type, a.duration, a.media_kind,
                   tc.id AS chunk_id, tc.start_s, tc.end_s, tc.text,
                   1 - vec_distance_cosine(v.embedding, :vector) AS score
            FROM transcript_chunks tc
            JOIN text_vec v ON v.rowid = tc.id
            JOIN assets a ON a.id = tc.asset_id
            JOIN sources s ON s.id = a.source_id
            WHERE {where} AND tc.model = :model
            ORDER BY score DESC
            LIMIT :limit""",
        {**params, "vector": to_blob(vector), "model": text.name, "limit": limit},
    )
    return [{**dict(r), "score": round(r["score"], 4), "match": "transcript"} for r in rows]


def search(conn: sqlite3.Connection, tiers: Sequence[str], query: str, today: date,
           limit: int, models: SearchModels | None) -> dict:  # fmt: skip
    where, params = _filters(tiers, today)
    query = query.strip()
    if not query:
        note = "every eligible clip; give a query to rank cutaways by what they show"
        return {"results": _listing(conn, where, params, "", limit), "spoken": [], "note": note}
    if models is None or (models.text is None and models.clip is None):
        note = "text match only on moments and file names: this run has no embedding models"
        return {"results": _listing(conn, where, params, query, limit), "spoken": [],
                "note": note}  # fmt: skip
    results = _by_image(conn, where, params, query, limit, models.clip) if models.clip else []
    spoken = _by_transcript(conn, where, params, query, limit, models.text) if models.text else []
    note = ("results: scenes ranked by what their keyframe shows (CLIP), the best scene of "
            "each clip; spoken: transcript passages ranked by meaning")  # fmt: skip
    return {"results": results, "spoken": spoken, "note": note}


# The music pool (FR-ING-6, ADR 0023) --------------------------------------------------------

MUSIC_ELIGIBLE = f"""a.rights_type IN ({{tiers}}) AND a.media_kind = 'audio'
      AND t.imported_at IS NOT NULL
      AND (a.licence_expires_at IS NULL OR a.licence_expires_at >= :today)
      AND (s.expires_at IS NULL OR s.expires_at >= :today)
      AND {LICENSED}"""


def _tempo_off(bpm: float | None, tempo: tuple[int, int] | None) -> float:
    """How far a track's tempo lies outside the asked range (0 inside it)."""
    if tempo is None:
        return 0.0
    if bpm is None:
        return 1000.0  # unmeasured tempos sort last
    low, high = tempo
    return max(low - bpm, bpm - high, 0.0)


def search_music(conn: sqlite3.Connection, tiers: Sequence[str], query: str, today: date,
                 limit: int, tempo: tuple[int, int] | None = None,
                 energy: str | None = None) -> dict:  # fmt: skip
    """Imported tracks a format may use, ordered by the energy asked, then nearness to the
    tempo range, then query words in the title or artist."""
    names = [f":t{n}" for n in range(len(tiers))]
    params = {"today": today.isoformat(), **{f"t{n}": t for n, t in enumerate(tiers)}}
    rows = conn.execute(
        f"""SELECT a.id AS asset_id, a.source_id, a.rights_type, a.duration, a.media_kind,
                   t.title, t.artist, t.tempo_bpm, t.energy, l.licence_url, l.attribution
            FROM tracks t
            JOIN assets a ON a.id = t.asset_id
            JOIN sources s ON s.id = a.source_id
            LEFT JOIN asset_licences l ON l.asset_id = a.id
            WHERE {MUSIC_ELIGIBLE.format(tiers=", ".join(names))}""",
        params,
    ).fetchall()
    words = set(query.lower().split())

    def order(row: sqlite3.Row) -> tuple:
        named = set(f"{row['title']} {row['artist'] or ''}".lower().split())
        missed = int(energy is not None and row["energy"] != energy)
        return (missed, _tempo_off(row["tempo_bpm"], tempo), -len(words & named),
                row["asset_id"])  # fmt: skip

    ranked = sorted(rows, key=order)[:limit]
    results = [{**dict(r), "tempo_off": round(_tempo_off(r["tempo_bpm"], tempo), 1),
                "match": "music"} for r in ranked]  # fmt: skip
    note = (
        "music: imported tracks, the energy asked first, then nearest the tempo range, "
        "then query words in the title or artist; credit each with its attribution"
    )
    return {"results": results, "spoken": [], "note": note}
