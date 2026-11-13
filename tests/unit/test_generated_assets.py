"""The engine's own media (voice WAVs, avatar videos) as assets of the built-in `engine`
source, so edit plans can name them and the rights check sees their tier."""

from engine.db import open_db
from engine.library.assets import ENGINE_SOURCE, register_generated


def test_engine_source_is_seeded_by_migration(tmp_path):
    conn = open_db(tmp_path / "engine.db")
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (ENGINE_SOURCE,)).fetchone()
    assert row["rights_type"] == "ai_generated"
    assert "ElevenLabs" in row["rights_proof"] and "HeyGen" in row["rights_proof"]


def test_register_generated_stores_an_ai_generated_asset_clear_of_people(tmp_path):
    conn = open_db(tmp_path / "engine.db")
    wav = tmp_path / "script-1.wav"
    wav.write_bytes(b"RIFF voice")
    asset_id = register_generated(conn, wav, "audio", duration=12.5)
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["source_id"] == ENGINE_SOURCE
    assert row["rights_type"] == "ai_generated"
    assert row["people_release"] == "none"  # Abbas's own clone and avatar (§8.3)
    assert row["media_kind"] == "audio" and row["duration"] == 12.5
    assert row["path"] == str(wav.resolve())


def test_registering_the_same_file_twice_returns_the_same_asset(tmp_path):
    conn = open_db(tmp_path / "engine.db")
    video = tmp_path / "7.mp4"
    video.write_bytes(b"avatar bytes")
    first = register_generated(conn, video, "video", duration=30.0, width=1080, height=1920)
    second = register_generated(conn, video, "video", duration=30.0, width=1080, height=1920)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1
