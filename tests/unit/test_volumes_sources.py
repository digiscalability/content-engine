"""Film drives (SRD §8.2): sources mirrored from sources.yaml, volumes matched by serial and
label (never by letter), and subtitle files read into timed cues."""

import sys
from dataclasses import replace

import pytest

from engine.config import load_config
from engine.config.models import SourceConfig
from engine.db import open_db
from engine.library.sources import sync_sources
from engine.library.subtitles import parse_srt, parse_subtitles, parse_vtt
from engine.library.volumes import Volume, find_volume, mounted_volumes, normalise_serial

FILMS = SourceConfig(
    id="films", kind="drive", rights_type="film_reference",
    rights_proof="Abbas's own film collection, used only under §8.2",
    volume_serial="1a2b-3c4d", volume_label="FILMS", path="Movies",
)  # fmt: skip


@pytest.fixture
def conn(tmp_path):
    return open_db(tmp_path / "engine.db")


def test_serials_compare_as_eight_hex_digits():
    assert normalise_serial("1a2b-3c4d") == normalise_serial("1A2B3C4D") == "1A2B3C4D"
    assert normalise_serial(" 2b-3c4d ") == "002B3C4D"


def test_the_drive_is_found_by_serial_and_label_not_letter(tmp_path):
    lookalike = Volume(tmp_path / "E", "1A2B-3C4D", "Backup")  # same serial, other label
    other = Volume(tmp_path / "F", "9999-0000", "FILMS")  # same label, other serial
    films = Volume(tmp_path / "G", "1A2B-3C4D", "films")
    assert find_volume(FILMS, [lookalike, other, films]) == films
    assert find_volume(FILMS, [lookalike, other]) is None


@pytest.mark.skipif(sys.platform != "win32", reason="volume serials are read on Windows")
def test_mounted_volumes_include_the_system_drive():
    roots = {str(v.root).upper() for v in mounted_volumes()}
    assert any(root.startswith("C:") for root in roots)


def test_sync_sources_mirrors_the_config(conn, repo_root):
    config = replace(load_config(repo_root / "config"), sources=[FILMS])
    sync_sources(conn, config)
    row = conn.execute("SELECT * FROM sources WHERE id = 'films'").fetchone()
    assert (row["kind"], row["volume_serial"], row["volume_label"], row["path"]) == (
        "drive",
        "1a2b-3c4d",
        "FILMS",
        "Movies",
    )
    renamed = FILMS.model_copy(update={"volume_label": "FILMS2"})
    sync_sources(conn, replace(config, sources=[renamed]))
    assert conn.execute("SELECT volume_label FROM sources WHERE id = 'films'").fetchone()[0] == (
        "FILMS2"
    )


def test_a_source_cannot_change_tier_once_it_has_assets(conn, repo_root):
    config = replace(load_config(repo_root / "config"), sources=[FILMS])
    sync_sources(conn, config)
    conn.execute(
        "INSERT INTO assets (source_id, sha256, path, media_kind, rights_type) "
        "VALUES ('films', ?, 'Movies/a.mkv', 'video', 'film_reference')",
        ("a" * 64,),
    )
    own = FILMS.model_copy(update={"rights_type": "own"})
    with pytest.raises(ValueError, match="films"):
        sync_sources(conn, replace(config, sources=[own]))


SRT = """1
00:00:01,000 --> 00:00:03,500
<i>You have to</i> start
somewhere.

2
00:01:02,250 --> 00:01:04,000
{\\an8}Then again.
"""

VTT = """WEBVTT

00:00:01.000 --> 00:00:03.500 align:start
You have to start somewhere.

01:02.250 --> 01:04.000
Then again.
"""


def test_srt_and_vtt_become_timed_cues():
    for cues in (parse_srt(SRT), parse_vtt(VTT)):
        assert [(c.text, c.start, c.end) for c in cues] == [
            ("You have to start somewhere.", 1.0, 3.5),
            ("Then again.", 62.25, 64.0),
        ]


def test_subtitle_files_are_read_by_suffix(tmp_path):
    srt = tmp_path / "film.en.srt"
    srt.write_bytes(SRT.encode("utf-8-sig"))  # a BOM, as many rippers write
    assert parse_subtitles(srt)[0].text == "You have to start somewhere."
    odd = tmp_path / "film.sub"
    odd.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="sub"):
        parse_subtitles(odd)


def test_sync_sources_needs_no_film_drive(conn, repo_root):
    sync_sources(conn, load_config(repo_root / "config"))  # one own folder, no film drive
    rows = conn.execute("SELECT id, rights_type FROM sources ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("engine", "ai_generated"), ("own-videos", "own")]
