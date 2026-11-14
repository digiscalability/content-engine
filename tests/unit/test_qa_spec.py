"""FR-QA-4 spec check: resolution, fps, duration, loudness, caption coverage and caption
safe zones, read from the render report against engine.yaml."""

import copy

import pytest

from engine.config import load_config
from engine.qa.spec import SpecRules, spec_check

GOOD = {
    "duration": 34.8,
    "plan_duration": 34.8,
    "video": {"width": 1080, "height": 1920, "fps": 30.0},
    "loudness": {"integrated_lufs": -14.2, "true_peak_dbtp": -1.6},
    "captions": {"coverage": 1.0, "box": [170, 1183, 905, 1275]},
}


@pytest.fixture
def rules(repo_root):
    return SpecRules.from_engine(load_config(repo_root / "config").engine, "C")


def check(rules, report=GOOD, platforms=("youtube",), **kw):
    options = {"speech": True, "captions": True, "silent_voice": False, **kw}
    return spec_check(report, rules, list(platforms), **options)


def changed(path: str, value) -> dict:
    report = copy.deepcopy(GOOD)
    *parents, last = path.split(".")
    node = report
    for key in parents:
        node = node[key]
    node[last] = value
    return report


def test_a_good_render_passes(rules):
    result = check(rules, platforms=("youtube", "tiktok", "instagram"))
    assert result.result == "pass", result.reasons


@pytest.mark.parametrize(
    ("path", "value", "words"),
    [
        ("video.width", 720, "720x1920"),
        ("video.fps", 25.0, "25"),
        ("duration", 75.0, "75"),
        ("plan_duration", 30.0, "plan"),
        ("loudness.integrated_lufs", -18.0, "-18"),
        ("loudness.true_peak_dbtp", -0.2, "true peak"),
        ("captions.coverage", 0.9, "0.90"),
    ],
)
def test_each_spec_limit_fails(rules, path, value, words):
    result = check(rules, changed(path, value))
    assert result.result == "fail" and words in " ".join(result.reasons)


def test_a_silent_mix_fails_unless_the_voice_is_a_silent_fake(rules):
    silent = changed("loudness.integrated_lufs", None)
    assert "silence" in " ".join(check(rules, silent).reasons)
    assert check(rules, silent, silent_voice=True).result == "pass"


def test_captions_under_the_button_rail_fail(rules):
    wide = changed("captions.box", [103, 1183, 980, 1275])  # P0.8's 900 px captions
    result = check(rules, wide)
    assert result.result == "fail" and "right" in result.reasons[0]
    assert "youtube" in result.reasons[0]


def test_captions_in_the_bottom_ui_fail(rules):
    low = changed("captions.box", [170, 1500, 905, 1700])
    assert "bottom" in " ".join(check(rules, low).reasons)


def test_speech_needs_visible_captions(rules):
    assert "captions" in check(rules, captions=False).reasons[0]
    invisible = changed("captions.box", None)
    assert "nothing visible" in check(rules, invisible).reasons[0]


def test_no_speech_needs_no_captions(rules):
    quiet = changed("captions", {"coverage": None, "box": None})
    assert check(rules, quiet, speech=False, captions=False).result == "pass"


def test_a_platform_without_a_safe_zone_holds(rules):
    result = check(rules, platforms=("myspace",))
    assert result.result == "hold" and "myspace" in result.reasons[0]


def test_a_missing_report_fails(rules):
    assert check(rules, None).result == "fail"
