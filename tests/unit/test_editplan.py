"""Edit plan schema (SRD §7.1): a JSON shot list that fully determines a render."""

import pytest
from pydantic import ValidationError

from engine.produce.editplan import EditPlan

PLAN = {
    "timeline": [
        {
            "asset_id": 1,
            "in": 0.0,
            "out": 2.0,
            "role": "avatar",
            "ops": {"zoom": {"from_scale": 1.0, "to_scale": 1.2, "ease": "in_out"}},
        },
        {"asset_id": 2, "in": 1.0, "out": 3.0, "role": "broll", "ops": {"speed": 2.0}},
        {
            "asset_id": 2,
            "in": 4.0,
            "out": 5.0,
            "role": "broll",
            "ops": {
                "freeze": {"at": 0.5, "duration": 1.0},
                "annotate": [{"block": "arrow-1", "start": 0.2, "duration": 1.0}],
            },
        },
        {"asset_id": 3, "in": 0.0, "out": 1.0, "role": "card"},
    ],
    "audio": {
        "voice": [{"asset_id": 10, "at": 1.0, "in": 0.0, "out": 3.0}],
        "music": {"asset_id": 11, "gain_db": -12, "duck_db": 12},
        "sfx": [{"asset_id": 12, "at": 2.0, "gain_db": -6}],
    },
    "overlays": [{"block": "lower-third", "kind": "lower_third", "start": 0.5, "duration": 2.0}],
    "captions": {"block": "captions-v1"},
    "look": {"id": "selfhelp", "version": 1},
}


def plan(**changes) -> dict:
    data = {**PLAN, **changes}
    return data


def test_plan_parses_srd_field_names():
    parsed = EditPlan.model_validate(PLAN)
    assert parsed.timeline[0].in_s == 0.0 and parsed.timeline[0].out_s == 2.0
    assert parsed.timeline[0].ops.zoom.to_scale == 1.2
    assert parsed.audio.music.duck_db == 12
    dumped = parsed.model_dump(by_alias=True, exclude_defaults=True)
    assert dumped["timeline"][0]["in"] == 0.0 and dumped["timeline"][0]["out"] == 2.0


def test_shot_duration_counts_speed_and_freeze():
    parsed = EditPlan.model_validate(PLAN)
    assert [s.duration for s in parsed.timeline] == [2.0, 1.0, 2.0, 1.0]
    assert parsed.timeline[1].played == 1.0
    assert parsed.duration == 6.0
    assert parsed.shot_starts() == [0.0, 2.0, 3.0, 5.0]


def test_out_must_follow_in():
    bad = {"asset_id": 1, "in": 2.0, "out": 2.0, "role": "avatar"}
    with pytest.raises(ValidationError, match="out"):
        EditPlan.model_validate(plan(timeline=[bad]))


def test_freeze_inside_played_part():
    bad = {
        "asset_id": 1,
        "in": 0.0,
        "out": 1.0,
        "role": "broll",
        "ops": {"freeze": {"at": 1.5, "duration": 1.0}},
    }
    with pytest.raises(ValidationError, match="freeze"):
        EditPlan.model_validate(plan(timeline=[bad], overlays=[], audio={}))


def test_overlapping_voice_rejected():
    voice = [{"asset_id": 10, "at": 0.0, "out": 2.0}, {"asset_id": 13, "at": 1.5, "out": 1.0}]
    with pytest.raises(ValidationError, match="overlap"):
        EditPlan.model_validate(plan(audio={"voice": voice}))


def test_voice_past_end_rejected():
    with pytest.raises(ValidationError, match="past the end"):
        EditPlan.model_validate(plan(audio={"voice": [{"asset_id": 10, "at": 5.0, "out": 2.0}]}))


def test_overlay_past_end_rejected():
    late = [{"block": "credits", "kind": "credit_card", "start": 5.0, "duration": 2.0}]
    with pytest.raises(ValidationError, match="past the end"):
        EditPlan.model_validate(plan(overlays=late))


def test_annotation_inside_its_shot():
    bad = {
        "asset_id": 1,
        "in": 0.0,
        "out": 1.0,
        "role": "evidence",
        "ops": {"annotate": [{"block": "a", "start": 0.5, "duration": 1.0}]},
    }
    with pytest.raises(ValidationError, match="annotation"):
        EditPlan.model_validate(plan(timeline=[bad], overlays=[], audio={}))


def test_annotations_become_absolute_overlays():
    placed = EditPlan.model_validate(PLAN).placed_overlays()
    assert [(o.block, o.kind, o.start, o.duration) for o in placed] == [
        ("lower-third", "lower_third", 0.5, 2.0),
        ("arrow-1", "annotation", 3.2, 1.0),
    ]


def test_unknown_keys_rejected():
    with pytest.raises(ValidationError, match="Extra inputs"):
        EditPlan.model_validate(plan(transitions=[]))


def test_only_cuts_for_now():
    shot = {**PLAN["timeline"][3], "transition": "crossfade"}
    with pytest.raises(ValidationError, match="transition"):
        EditPlan.model_validate(plan(timeline=[shot], overlays=[], audio={}))
