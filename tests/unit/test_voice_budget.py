"""Voice credit preflight and ledger (D10, C16, NFR-1, NFR-4) with FakeVoice and a real DB."""

import pytest

from engine.db import open_db
from engine.produce.voice import (
    Balance,
    FakeVoice,
    VoiceBudgetError,
    VoiceRequest,
    engine_characters,
    preflight,
    synthesize_batch,
)

CAP, FLOOR, MONTH = 78_600, 20_000, "2026-11"


def req(script_id: int, chars: int = 700) -> VoiceRequest:
    return VoiceRequest(script_id=script_id, text="x" * chars, voice_id="voice-abbas")


@pytest.fixture
def db(tmp_path):
    return open_db(tmp_path / "engine.db")


def ledger(db) -> list[tuple]:
    rows = db.execute("SELECT vendor, month, units, charge_key FROM budget_ledger ORDER BY id")
    return [tuple(r) for r in rows]


def test_preflight_passes_inside_both_limits():
    preflight(700, 50_000, Balance(used=60_000, limit=131_000), CAP, FLOOR)


def test_preflight_stops_at_the_engine_cap():
    with pytest.raises(VoiceBudgetError, match="engine cap"):
        preflight(700, 78_000, Balance(used=78_000, limit=131_000), CAP, FLOOR)


def test_preflight_stops_at_the_account_floor():
    with pytest.raises(VoiceBudgetError, match="account floor"):
        preflight(700, 10_000, Balance(used=110_500, limit=131_000), CAP, FLOOR)


def test_batch_writes_one_ledger_row_per_script(db, tmp_path):
    voice = FakeVoice()
    paths = synthesize_batch([req(1), req(2, 500)], voice, db, tmp_path, CAP, FLOOR, MONTH)
    assert set(paths) == {1, 2} and all(p.is_file() for p in paths.values())
    assert ledger(db) == [
        ("elevenlabs", MONTH, 700.0, "voice:script:1"),
        ("elevenlabs", MONTH, 500.0, "voice:script:2"),
    ]
    assert engine_characters(db, MONTH) == 1200


def test_batch_preflights_before_any_call(db, tmp_path):
    db.execute(
        "INSERT INTO budget_ledger (vendor, month, units, charge_key) "
        "VALUES ('elevenlabs', ?, 78000, 'earlier')",
        (MONTH,),
    )
    voice = FakeVoice()
    with pytest.raises(VoiceBudgetError):
        synthesize_batch([req(1), req(2)], voice, db, tmp_path, CAP, FLOOR, MONTH)
    assert voice.calls == []


def test_resumed_batch_never_charges_twice(db, tmp_path):
    voice = FakeVoice()
    synthesize_batch([req(1)], voice, db, tmp_path, CAP, FLOOR, MONTH)
    synthesize_batch([req(1)], voice, db, tmp_path, CAP, FLOOR, MONTH)
    assert len(voice.calls) == 1
    # a crash after the WAV was written but before its ledger row: no new call, row backfilled
    db.execute("DELETE FROM budget_ledger")
    synthesize_batch([req(1)], voice, db, tmp_path, CAP, FLOOR, MONTH)
    assert len(voice.calls) == 1
    assert ledger(db) == [("elevenlabs", MONTH, 700.0, "voice:script:1")]


def test_engine_characters_counts_only_this_month_and_vendor(db):
    rows = [
        ("elevenlabs", "2026-11", 700, "a"),
        ("elevenlabs", "2026-10", 900, "b"),
        ("heygen", "2026-11", 5, "c"),
    ]
    for row in rows:
        db.execute(
            "INSERT INTO budget_ledger (vendor, month, units, charge_key) VALUES (?, ?, ?, ?)", row
        )
    assert engine_characters(db, "2026-11") == 700
