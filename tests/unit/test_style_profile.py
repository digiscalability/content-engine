"""`ce refs analyze` (SRD §9.1, C10): references measured one by one, aggregated into one
style profile, stored, and only then deleted."""

import json
import sqlite3

import pytest

from engine.agents.state import sync_identities
from engine.config import load_config
from engine.db import open_db
from engine.style.profile import RefMetrics, aggregate, analyze_refs, reference_files


def metrics(name: str, **values) -> RefMetrics:
    base = {
        "file": name, "duration": 30.0, "width": 1080, "height": 1920, "shots": 10,
        "cuts_per_min": 18.0, "shot_median_s": 2.5, "shot_p10_s": 1.0, "shot_p90_s": 5.0,
        "first_word_s": 0.4, "words_per_s": 2.6, "first_text_s": 0.5, "text_frame_share": 0.9,
        "caption_y": 0.7, "caption_height": 0.05, "words_per_page": 3, "loudness_lufs": -14.0,
        "loudness_range": 6.0, "tempo_bpm": 100.0, "brightness": 0.5, "saturation": 0.4,
        "palette": ["#101010", "#f0f0f0", "#ffcc00", "#3366ff", "#aa2222"],
    }  # fmt: skip
    return RefMetrics(**{**base, **values})


def test_aggregate_takes_medians_and_skips_missing_values():
    refs = [metrics("a.mp4", cuts_per_min=10.0, first_word_s=None),
            metrics("b.mp4", cuts_per_min=20.0, first_word_s=0.2),
            metrics("c.mp4", cuts_per_min=30.0, first_word_s=0.6)]  # fmt: skip
    profile = aggregate(refs)
    assert profile["metrics"]["cuts_per_min"] == 20.0
    assert profile["metrics"]["first_word_s"] == pytest.approx(0.4)
    assert profile["ref_count"] == 3 and len(profile["palette"]) <= 5
    assert "file" not in profile["metrics"]  # only numbers and the palette are kept (C10)


def test_reference_files_are_videos_only(tmp_path):
    for name in ("b.mp4", "a.MOV", "c.webm", "notes.txt", ".gitkeep"):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in reference_files(tmp_path)] == ["a.MOV", "b.mp4", "c.webm"]


@pytest.fixture
def conn(tmp_path, repo_root):
    db = open_db(tmp_path / "engine.db")
    sync_identities(db, load_config(repo_root / "config"))
    return db


def inbox(tmp_path, names=("one.mp4", "two.mp4")):
    folder = tmp_path / "references" / "selfhelp"
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_bytes(b"video")
    return folder


def test_refs_are_deleted_only_after_the_profile_is_stored(conn, tmp_path):
    folder = inbox(tmp_path)
    profile = analyze_refs(conn, "selfhelp", folder, measure=lambda p: metrics(p.name))
    row = conn.execute("SELECT * FROM style_profiles").fetchone()
    assert row["identity_id"] == "selfhelp" and row["ref_count"] == 2
    assert json.loads(row["metrics"])["cuts_per_min"] == 18.0
    assert json.loads(row["palette"]) == profile["palette"]
    assert list(folder.iterdir()) == []  # C10: the files go once their numbers are stored


def test_a_failed_store_keeps_every_reference(conn, tmp_path):
    folder = inbox(tmp_path)
    conn.execute("DROP TABLE style_profiles")  # the store will fail
    with pytest.raises(sqlite3.OperationalError):
        analyze_refs(conn, "selfhelp", folder, measure=lambda p: metrics(p.name))
    assert sorted(p.name for p in folder.iterdir()) == ["one.mp4", "two.mp4"]


def test_keep_leaves_the_references(conn, tmp_path):
    folder = inbox(tmp_path)
    analyze_refs(conn, "selfhelp", folder, measure=lambda p: metrics(p.name), keep=True)
    assert len(list(folder.iterdir())) == 2


def test_an_empty_inbox_is_an_error_that_names_g0_8(conn, tmp_path):
    folder = inbox(tmp_path, names=())
    with pytest.raises(ValueError, match="G0.8"):
        analyze_refs(conn, "selfhelp", folder, measure=lambda p: metrics(p.name))
