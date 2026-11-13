"""C7, §7.4: every render exports OTIO and FCPXML so Abbas can polish it in DaVinci
Resolve. OTIO is read back with opentimelineio; FCPXML is parsed and read back with the
OTIO FCPXML adapter (a dev dependency)."""

import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import opentimelineio as otio
import pytest

from engine.produce.editplan import EditPlan
from engine.produce.exports import ExportMedia, plan_timeline, write_fcpxml, write_otio

FPS = 30
PLAN = EditPlan.model_validate(
    {
        "timeline": [
            {"asset_id": 1, "in": 0.0, "out": 2.0, "role": "avatar",
             "ops": {"zoom": {"from_scale": 1.0, "to_scale": 1.3}}},
            {"asset_id": 2, "in": 1.0, "out": 3.0, "role": "broll", "ops": {"speed": 2.0}},
            {"asset_id": 2, "in": 3.0, "out": 4.0, "role": "broll",
             "ops": {"freeze": {"at": 0.5, "duration": 1.0}}},
            {"asset_id": 1, "in": 4.0, "out": 5.0, "role": "avatar"},
        ],
        "audio": {
            "voice": [{"asset_id": 10, "at": 0.5, "in": 0.0, "out": 3.0}],
            "music": {"asset_id": 11, "gain_db": -18, "duck_db": 12},
            "sfx": [{"asset_id": 12, "at": 2.0}],
        },
        "overlays": [{"block": "lower-third", "kind": "lower_third", "start": 1.0,
                      "duration": 2.0}],
        "captions": {"block": "captions"},
        "look": {"id": "selfhelp", "version": 1},
    }
)  # fmt: skip
MEDIA = ExportMedia(
    assets={i: Path(f"C:/media/a{i}.{'mp4' if i < 10 else 'wav'}") for i in (1, 2, 10, 11, 12)},
    durations={1: 10.0, 2: 6.0, 10: 3.0, 11: 4.0, 12: 0.2},
    sizes={1: (1080, 1920), 2: (1920, 1080)},
    captions=Path("C:/work/captions.mov"),
    overlays={"lower-third": Path("C:/work/lower third.mov")},
)  # fmt: skip
TOTAL = round(PLAN.duration * FPS)  # 2 + 1 + (1 + 1 held) + 1 = 6 s, 180 frames


def frames(t) -> int:
    return round(t.to_frames()) if hasattr(t, "to_frames") else t


def track(timeline, name):
    return next(t for t in timeline.tracks if t.name == name)


def test_otio_picture_track_matches_the_plan_frame_for_frame(tmp_path):
    path = write_otio(plan_timeline(PLAN, MEDIA, "post", FPS), tmp_path / "post.otio")
    timeline = otio.adapters.read_from_file(str(path))
    picture = track(timeline, "V1 picture")
    assert picture.duration().to_frames() == TOTAL
    pieces = [(c.source_range.start_time.to_frames(), c.source_range.duration.to_frames(),
               [e.schema_name() for e in c.effects]) for c in picture]  # fmt: skip
    assert pieces == [
        (0, 60, []),
        (30, 30, ["LinearTimeWarp"]),  # 2 s of source at 2x: 1 s on the timeline
        (90, 15, []),  # the freeze splits the shot: 0.5 s played,
        (105, 30, ["FreezeFrame"]),  # 1 s held on its last frame,
        (105, 15, []),  # then the rest
        (120, 30, []),
    ]
    zoomed = picture[0]
    assert zoomed.metadata["content_engine"]["ops"]["zoom"]["to_scale"] == 1.3
    assert "zoom" in zoomed.markers[0].name
    assert zoomed.media_reference.target_url == Path("C:/media/a1.mp4").as_uri()


def test_otio_layers_sit_at_their_plan_times(tmp_path):
    timeline = plan_timeline(PLAN, MEDIA, "post", FPS)

    def placed(name):
        t = track(timeline, name)
        return [(t.range_of_child_at_index(i).start_time.to_frames(), c.duration().to_frames())
                for i, c in enumerate(t) if isinstance(c, otio.schema.Clip)]  # fmt: skip

    assert placed("V2 captions") == [(0, TOTAL)]
    assert placed("V3 overlays") == [(30, 60)]
    assert placed("A1 voice") == [(15, 90)]
    assert placed("A2 music") == [(0, TOTAL)]
    assert placed("A3 sfx") == [(60, 6)]
    assert track(timeline, "A1 voice").kind == otio.schema.TrackKind.Audio


def parse_time(text: str) -> Fraction:
    return Fraction(text.removesuffix("s")) if text != "0s" else Fraction(0)


def test_fcpxml_spine_is_contiguous_and_frame_aligned(tmp_path):
    path = write_fcpxml(PLAN, MEDIA, "post", FPS, tmp_path / "post.fcpxml")
    root = ET.parse(path).getroot()
    assert root.tag == "fcpxml" and root.get("version") == "1.8"
    ids = {e.get("id") for e in root.find("resources")}
    sequence = root.find("library/event/project/sequence")
    assert parse_time(sequence.get("duration")) * FPS == TOTAL
    offset = Fraction(0)
    for clip in sequence.find("spine"):
        assert parse_time(clip.get("offset")) == offset
        for attr in ("offset", "start", "duration"):
            assert (parse_time(clip.get(attr, "0s")) * FPS).denominator == 1  # whole frames
        assert clip.get("ref") in ids
        offset += parse_time(clip.get("duration"))
    assert offset * FPS == TOTAL
    lanes = sorted(int(c.get("lane")) for c in sequence.iter("asset-clip") if c.get("lane"))
    assert lanes == [-3, -2, -1, 1, 2]  # sfx, music, voice; captions, overlay
    formats = {f.get("id"): (f.get("width"), f.get("height")) for f in root.iter("format")}
    assert all(c.get("format") is None for c in sequence.find("spine"))  # clips keep their own
    assert ("1080", "1920") in formats.values() and ("1920", "1080") in formats.values()


def test_fcpxml_connected_clips_land_at_their_timeline_times(tmp_path):
    path = write_fcpxml(PLAN, MEDIA, "post", FPS, tmp_path / "post.fcpxml")
    spine = ET.parse(path).getroot().find("library/event/project/sequence/spine")
    placed = {}
    for parent in spine:
        p_offset, p_start = parse_time(parent.get("offset")), parse_time(parent.get("start", "0s"))
        for child in parent.findall("asset-clip"):
            placed[child.get("name")] = (p_offset + parse_time(child.get("offset")) - p_start) * FPS
    assert placed == {"captions": 0, "lower-third": 30, "a10": 15, "a11": 0, "a12": 60}


def test_fcpxml_reads_back_with_the_otio_adapter(tmp_path):
    pytest.importorskip("otio_fcpx_xml_adapter")
    path = write_fcpxml(PLAN, MEDIA, "post", FPS, tmp_path / "post.fcpxml")
    read = otio.adapters.read_from_file(str(path), adapter_name="fcpx_xml")
    (timeline,) = read if isinstance(read, otio.schema.SerializableCollection) else [read]
    assert timeline.name == "post" and timeline.duration().to_frames() == TOTAL
