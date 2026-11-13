"""Text checks FR-QA-6 (banned topics and claims) and FR-QA-8 (claims about Abbas), on the
script, captions, titles and hashtags. A made-up sheet stands in for the Public Fact Sheet:
public test files never repeat its never-list (the sheet's own rule)."""

import pytest

from engine.qa.text import (
    FactSheet,
    banned_terms_check,
    claims_check,
    post_texts,
)

SHEET = """# Public Fact Sheet

## Who
- Public name: **Abbas Ali**. Location: Melbourne, Victoria.
- Solo founder: one person. Never "we", "our team" or "studio".
- Never mention pets or hobbies, a cousin, the current side gig ("Side gig in art" is false), \
past employers or awards. That includes public test files.
- Never use "guru".
- Homepage byline: "Built and run by Abbas Ali, software engineer and former chef, Melbourne."

## Products
- Never call the demo "live AI agents".
"""
SELF = ["Abbas", "chef", "engineer", "developer", "founder"]
NEVER = ["mastermind"]  # extra terms from the identity config
BANNED = {
    "medical": ["cure", "diagnosis", "medication"],
    "money": ["invest", "investing", "passive income", "get rich"],
}


@pytest.fixture
def facts(tmp_path) -> FactSheet:
    path = tmp_path / "Public Fact Sheet.md"
    path.write_text(SHEET, encoding="utf-8")
    return FactSheet.load(path)


def claims(texts, facts):
    return claims_check([("script", t) for t in texts], facts, SELF, NEVER)


def test_the_srd_acceptance_pair(facts):
    failed = claims(["When I worked at a restaurant in Sydney, the rush taught patience."], facts)
    assert failed.result == "fail" and any("first person" in r for r in failed.reasons)
    passed = claims(["Abbas, a former chef, keeps the first step small."], facts)
    assert passed.result == "pass", passed.reasons


def test_a_role_or_place_not_in_the_fact_sheet_fails(facts):
    result = claims(["Abbas, a head chef in Sydney, starts small."], facts)
    assert result.result == "fail"
    assert any("'head chef'" in r for r in result.reasons)
    assert any("'Sydney'" in r for r in result.reasons)


def test_fact_sheet_wording_passes(facts):
    result = claims(["Abbas is a software engineer and former chef in Melbourne."], facts)
    assert result.result == "pass", result.reasons


def test_the_never_list_comes_from_the_fact_sheet_who_section(facts):
    assert facts.never_terms == [
        "we", "our team", "studio", "pets", "pet", "hobbies", "hobby", "cousin", "side gig",
        "past employers", "past employer", "awards", "award", "guru",
    ]  # fmt: skip


def test_never_list_terms_fail(facts):
    result = claims(["Your cousin does not decide your discipline."], facts)
    assert result.result == "fail" and any("'cousin'" in r for r in result.reasons)
    extra = claims(["Join the mastermind."], facts)  # from the identity config
    assert extra.result == "fail" and any("'mastermind'" in r for r in extra.reasons)
    assert claims(["Call it live practice."], facts).result == "pass"  # Products: not personal


def test_claims_without_a_fact_sheet_hold(tmp_path):
    missing = FactSheet.load(tmp_path / "nowhere.md")
    assert missing is None
    held = claims(["Abbas, a former chef, starts small."], missing)
    assert held.result == "hold" and "Fact Sheet" in held.reasons[0]
    assert claims(["Start smaller than you think."], missing).result == "pass"


def test_banned_terms_fail_with_their_category():
    texts = [("title", "Get rich slowly"), ("hashtags", "#PassiveIncome #focus")]
    result = banned_terms_check(texts, BANNED)
    assert result.result == "fail"
    assert any("money" in r and "get rich" in r for r in result.reasons)
    assert any("passive income" in r and "hashtags" in r for r in result.reasons)


def test_banned_terms_match_whole_words_only():
    texts = [("script", "Secure the first step; curiosity helps.")]
    assert banned_terms_check(texts, BANNED).result == "pass"


def test_post_texts_cover_script_captions_and_copy():
    beats = [{"text": "Start small.", "on_screen": "Small", "target_s": 5}]
    copy = [{"platform": "youtube", "title": "T", "caption": "C", "hashtags": ["#a", "#b"]}]
    texts = post_texts(beats, ["Start", "small."], copy)
    assert ("script", "Start small.") in texts and ("on-screen text", "Small") in texts
    assert ("captions", "Start small.") in texts
    assert ("youtube title", "T") in texts and ("youtube hashtags", "#a #b") in texts


def test_words_ending_in_us_or_ss_have_no_made_up_singular():
    from engine.qa.text import parse_never_terms

    assert parse_never_terms("## Who\n- Never mention a status, a bonus or a boss.") == [
        "status", "bonus", "boss",
    ]  # fmt: skip
