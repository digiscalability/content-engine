"""FR-QA-1 rights and FR-QA-5 disclosure (SRD §6.7, §8.1-§8.3).

`rights_violations()` is the one rights check: `submit_edit_plan`, hook H4 and FR-QA-1 all
use it (ADR 0011). FR-QA-1 runs it again on the rendered plan, because an asset's release
or licence can change between planning and rendering.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date

from engine.agents.schemas import allowed_tiers
from engine.library.search import LICENSED
from engine.produce.editplan import FILM_EXCERPT_MAX_S, FILM_SHARE_MAX, EditPlan
from engine.qa import CheckResult, verdict

CLEAR_PEOPLE = ("none", "yes")  # §8.3: no unreleased faces
# FR-PUB-3, ADR 0002: how each platform carries the AI label for an avatar post
DISCLOSURE_ROUTES = {
    "youtube": "uploaded private; Abbas ticks altered or synthetic content in Studio",
    "tiktok": "the video_made_with_ai setting on a direct post",
}


def rights_violations(conn: sqlite3.Connection, plan: EditPlan, fmt: str, today: date) -> list[str]:
    tiers = allowed_tiers(fmt)
    problems: list[str] = []
    film_seconds = 0.0
    audio_ids = [v.asset_id for v in plan.audio.voice] + [s.asset_id for s in plan.audio.sfx]
    if plan.audio.music:
        audio_ids.append(plan.audio.music.asset_id)
    for where, asset_id, shot in [
        *[(f"shot {i + 1}", s.asset_id, s) for i, s in enumerate(plan.timeline)],
        *[("audio", a, None) for a in audio_ids],
    ]:
        row = conn.execute(
            "SELECT a.rights_type, a.people_release, a.media_kind, a.licence_expires_at, "
            f"s.expires_at AS source_expires, {LICENSED} AS licensed "
            "FROM assets a JOIN sources s ON s.id = a.source_id WHERE a.id = ?",
            (asset_id,),
        ).fetchone()
        if row is None:
            problems.append(f"{where}: no such asset {asset_id}")
            continue
        if not row["licensed"]:
            problems.append(f"{where}: asset {asset_id} has no licence record (FR-ING-5)")
        if row["rights_type"] not in tiers:
            problems.append(f"{where}: {row['rights_type']} footage is not allowed in format {fmt}")
        for expiry in (row["licence_expires_at"], row["source_expires"]):
            if expiry and expiry < today.isoformat():
                problems.append(f"{where}: licence for asset {asset_id} expired {expiry}")
        if row["media_kind"] != "audio" and row["people_release"] not in CLEAR_PEOPLE:
            release = row["people_release"]
            problems.append(
                f"{where}: asset {asset_id} shows people without a release ({release}, §8.3)"
            )
        if row["rights_type"] == "film_reference" and shot is None:
            problems.append(f"{where}: film audio is never a voice, music or SFX source (§8.2)")
        if shot is not None and row["rights_type"] == "film_reference":
            # on-screen time counts: slow motion or a freeze must not stretch an excerpt
            excerpt = max(shot.out_s - shot.in_s, shot.duration)
            film_seconds += shot.duration
            if shot.role != "evidence":
                problems.append(f"{where}: film excerpts are evidence only, never B-roll (§8.2)")
            if excerpt > FILM_EXCERPT_MAX_S:
                problems.append(f"{where}: film excerpt runs {excerpt:g} s, over 10 s (§8.2)")
    if plan.duration and film_seconds / plan.duration > FILM_SHARE_MAX:
        problems.append(
            f"film excerpts fill {film_seconds / plan.duration:.0%} of the runtime; "
            "the limit is 30% (§8.2)"
        )
    return problems


def rights_check(conn: sqlite3.Connection, plan: EditPlan, fmt: str, today: date) -> CheckResult:
    return verdict("rights", [f"{p} (FR-QA-1)" for p in rights_violations(conn, plan, fmt, today)])


def needs_disclosure(conn: sqlite3.Connection, plan: EditPlan) -> bool:
    """An avatar shot, or any AI-generated picture on screen (FR-QA-5)."""
    for shot in plan.timeline:
        row = conn.execute(
            "SELECT rights_type, media_kind FROM assets WHERE id = ?", (shot.asset_id,)
        ).fetchone()
        if shot.role == "avatar" or (row and row["rights_type"] == "ai_generated"):
            return True
    return False


def disclosure_check(
    conn: sqlite3.Connection, plan: EditPlan, posts: Sequence[sqlite3.Row]
) -> CheckResult:
    """Every post of an AI-made render is marked for disclosure, on a channel that can carry
    the label (FR-QA-5, FR-PUB-3)."""
    if not needs_disclosure(conn, plan):
        return verdict("disclosure", [])
    fails = []
    for post in posts:
        if not post["ai_disclosure"]:
            fails.append(f"post {post['id']} ({post['platform']}) is not marked AI-disclosed")
        if post["platform"] not in DISCLOSURE_ROUTES:
            fails.append(
                f"post {post['id']}: {post['platform']} has no AI-label route for avatar posts "
                "yet (ADR 0002)"
            )
    return verdict("disclosure", [f"{f} (FR-QA-5)" for f in fails])
