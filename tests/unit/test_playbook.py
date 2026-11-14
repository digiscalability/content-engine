"""The playbook loader (FR-PLN-1): the Markdown playbook as structured data. The topic bank
is part of Abbas's Rules section."""

import pytest

from engine.config import load_config
from engine.plan.playbook import PlaybookError, load_playbook, parse_playbook
from engine.qa.text import banned_terms_check, claims_check

SAMPLE = """# x playbook

## Rules (Abbas-owned)

### Pillars

- Habits
- Craft: standards and repetition

### Banned topics and claims (FR-QA-6)

- Politics
- Religion

### Topic bank

Intro text the loader ignores.

#### Habits
- Never miss twice
- Shrink the step

#### Craft
- Clean as you go

#### Explore
- What chess openings teach about routines

## Learned (retro-owned)

Hooks that open on a question do better.
"""


def test_parse_reads_pillars_banned_topics_and_learned():
    book = parse_playbook(SAMPLE)
    assert book.pillars == ["Habits", "Craft"]
    assert book.banned == ["Politics", "Religion"]
    assert [(t.text, t.pillar, t.explore) for t in book.topics] == [
        ("Never miss twice", "Habits", False),
        ("Shrink the step", "Habits", False),
        ("Clean as you go", "Craft", False),
        ("What chess openings teach about routines", None, True),
    ]
    assert book.learned == "Hooks that open on a question do better."
    assert "### Pillars" in book.rules


def test_missing_section_is_named():
    with pytest.raises(PlaybookError, match="Topic bank"):
        parse_playbook(SAMPLE.replace("### Topic bank", "### Topics"))
    with pytest.raises(PlaybookError, match="Learned"):
        parse_playbook(SAMPLE.split("## Learned")[0])


def test_a_topic_under_an_unknown_pillar_is_refused():
    with pytest.raises(PlaybookError, match="Cooking"):
        parse_playbook(SAMPLE.replace("#### Craft", "#### Cooking"))


@pytest.fixture(scope="module")
def selfhelp(repo_root=None):
    from engine.settings import REPO_ROOT

    config = load_config(REPO_ROOT / "config")
    identity = config.identities["selfhelp"]
    return identity, load_playbook(REPO_ROOT / "config" / identity.playbook)


def test_the_selfhelp_bank_covers_every_pillar(selfhelp):
    _, book = selfhelp
    assert len(book.pillars) == 5
    for pillar in book.pillars:
        assert sum(t.pillar == pillar for t in book.topics) >= 3, pillar
    assert sum(t.explore for t in book.topics) >= 3
    assert len({t.text for t in book.topics}) == len(book.topics)  # no duplicates


def test_every_bank_topic_passes_the_text_checks(selfhelp):
    identity, book = selfhelp
    texts = [("topic bank", t.text) for t in book.topics]
    assert banned_terms_check(texts, identity.qa.banned_terms).result == "pass"
    claims = claims_check(texts, None, identity.qa.self_reference_terms, identity.qa.never_terms)
    assert claims.result == "pass", claims.reasons
