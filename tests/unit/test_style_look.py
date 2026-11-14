"""Looks (SRD §9.2): a versioned YAML file of grade, captions, title card, pacing and music
feel. The grade becomes a generated 3D LUT; nothing third-party is loaded."""

import pytest
from pydantic import ValidationError

from engine.produce.editplan import LookRef
from engine.style.look import (
    DEFAULT_LOOK,
    Grade,
    LookConfig,
    apply_grade,
    caption_style,
    find_look,
    grade_lut,
    load_look,
    look_path,
    save_look,
)


def look(**overrides) -> LookConfig:
    data = DEFAULT_LOOK.model_dump()
    data.update({"id": "selfhelp", "version": 1, "name": "Warm craft", "rationale": "test"})
    data.update(overrides)
    return LookConfig.model_validate(data)


def test_a_look_round_trips_through_yaml(tmp_path):
    warm = look(grade={"temperature": 0.06, "saturation": 1.1})
    path = save_look(warm, tmp_path)
    assert path == tmp_path / "selfhelp-v1.yaml"
    assert load_look(path) == warm


def test_save_never_overwrites_a_version(tmp_path):
    save_look(look(), tmp_path)
    with pytest.raises(FileExistsError):
        save_look(look(name="Changed"), tmp_path)


def test_find_look_reads_the_plan_reference_or_returns_none(tmp_path):
    save_look(look(), tmp_path)
    assert find_look([tmp_path], LookRef(id="selfhelp", version=1)).name == "Warm craft"
    assert find_look([tmp_path], LookRef(id="selfhelp", version=2)) is None
    assert look_path(tmp_path, "selfhelp", 3).name == "selfhelp-v3.yaml"


def test_the_neutral_grade_is_the_identity_lut():
    for rgb in [(0.0, 0.0, 0.0), (0.25, 0.5, 0.75), (1.0, 1.0, 1.0)]:
        assert apply_grade(Grade(), rgb) == pytest.approx(rgb, abs=1e-9)


def test_a_warm_grade_lifts_red_and_cuts_blue():
    r, g, b = apply_grade(Grade(temperature=0.1), (0.5, 0.5, 0.5))
    assert r > 0.5 > b and g == pytest.approx(0.5)


@pytest.mark.parametrize(
    "grade",
    [
        {"temperature": 0.2, "contrast": 1.4, "saturation": 1.6, "lift": 0.1, "gamma": 0.7},
        {"temperature": -0.2, "tint": -0.1, "contrast": 0.7, "saturation": 0.0, "lift": -0.1},
    ],
)
def test_grade_lut_stays_in_range(grade):
    text = grade_lut(Grade(**grade), size=9)
    rows = [line.split() for line in text.splitlines() if line[:1].isdigit()]
    assert len(rows) == 9**3
    assert all(0.0 <= float(v) <= 1.0 for row in rows for v in row)
    assert "LUT_3D_SIZE 9" in text


def test_caption_style_and_word_count_come_from_the_look():
    style, max_words = caption_style(look(captions={**DEFAULT_LOOK.captions.model_dump(),
                                                    "font_family": "Montserrat",
                                                    "max_words": 3}))  # fmt: skip
    assert style.font_family == "Montserrat" and max_words == 3


def test_fonts_must_be_bundled_with_hyperframes():
    with pytest.raises(ValidationError, match="font_family"):
        look(captions={**DEFAULT_LOOK.captions.model_dump(), "font_family": "Comic Sans MS"})


def test_pacing_ranges_are_ordered():
    with pytest.raises(ValidationError, match="cut_every_s"):
        look(pacing={"cut_every_s": [8, 5], "first_cut_by_s": 3})
