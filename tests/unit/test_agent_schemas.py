"""Submission schemas and the RunReport (SRD §5.4, §5.6, FR-PLN-3, FR-PRD-1, FR-QA-7)."""

import pytest
from pydantic import ValidationError

from engine.agents.schemas import (
    FORMAT_SECONDS,
    BriefSubmission,
    QAReviewSubmission,
    RunReport,
    ScriptSubmission,
    allowed_tiers,
)

BRIEF = {
    "identity_id": "selfhelp",
    "format": "C",
    "topic": "focus",
    "angle": "craft as repetition",
    "hook": "One habit beats ten plans",
    "cta": "follow",
    "source_moments": [],
    "difference_note": "First post on focus; list structure instead of story",
    "exploration": False,
}


def test_brief_needs_a_difference_note():
    BriefSubmission.model_validate(BRIEF)
    for bad in ("", "   "):
        with pytest.raises(ValidationError, match="difference_note"):
            BriefSubmission.model_validate({**BRIEF, "difference_note": bad})
    with pytest.raises(ValidationError, match="difference_note"):
        BriefSubmission.model_validate({k: v for k, v in BRIEF.items() if k != "difference_note"})


def test_script_reading_speed_is_capped_at_3_words_a_second():
    ok = {"brief_id": 1, "beats": [{"text": "one two three four five six", "target_s": 2.0}]}
    assert ScriptSubmission.model_validate(ok).duration == 2.0
    fast = {
        "brief_id": 1,
        "beats": [{"text": "one two three four five six seven", "target_s": 2.0}],
    }
    with pytest.raises(ValidationError, match="words a second"):
        ScriptSubmission.model_validate(fast)


def test_script_counts_words_and_duration():
    script = ScriptSubmission.model_validate(
        {
            "brief_id": 1,
            "beats": [
                {"text": "Discipline is a skill.", "on_screen": "SKILL", "target_s": 2.5},
                {"text": "You can practise it.", "target_s": 2.0},
            ],
        }
    )
    assert (script.words, script.duration) == (8, 4.5)


def test_hold_and_fail_reviews_need_reasons():
    QAReviewSubmission.model_validate({"render_id": 1, "verdict": "pass"})
    with pytest.raises(ValidationError, match="reasons"):
        QAReviewSubmission.model_validate({"render_id": 1, "verdict": "hold"})


def test_format_lengths_and_tiers_follow_the_srd():
    assert FORMAT_SECONDS == {"A": (30, 75), "B": (20, 60), "C": (30, 60)}
    assert "film_reference" in allowed_tiers("A")
    assert "film_reference" not in allowed_tiers("B")
    assert "film_reference" not in allowed_tiers("C")


def test_run_report_has_the_section_5_6_fields():
    schema = RunReport.model_json_schema()
    assert set(schema["required"]) >= {
        "run_id",
        "identity_results",
        "budget_used",
        "warnings",
        "needs_abbas",
    }
    report = RunReport.model_validate(
        {
            "run_id": "r1",
            "identity_results": [
                {"identity": "selfhelp", "posts_drafted": 1, "posts_failed": [], "qa_holds": []}
            ],
            "budget_used": {"claude_usd": 0.4, "elevenlabs_chars": 700, "heygen_credits": 0},
            "warnings": [],
            "needs_abbas": [{"item": "Schedule draft", "link": "https://x"}],
        }
    )
    assert report.identity_results[0].posts_drafted == 1
