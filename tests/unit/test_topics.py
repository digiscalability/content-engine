"""Topic planning (FR-PLN-1, FR-PLN-2): the next format, whether this post explores, and
which bank topics to offer."""

import pytest

from engine.plan.playbook import parse_playbook
from engine.plan.topics import exploration_due, next_format, topic_candidates

BOOK = parse_playbook(
    """## Rules (Abbas-owned)

### Pillars
- Habits
- Craft
- Focus

### Banned topics and claims
- Politics

### Topic bank
#### Habits
- Never miss twice
- Shrink the step
#### Craft
- Clean as you go
- Taste as you go
#### Focus
- One tab, one task
#### Explore
- Chess openings and routines
- Silence as a tool

## Learned (retro-owned)
"""
)


def texts(candidates) -> list[str]:
    return [t.text for t in candidates]


def test_next_format_follows_the_share_deficit():
    shares = {"C": 0.6, "A": 0.2, "B": 0.2}
    assert next_format(shares, []) == "C"
    assert next_format(shares, ["C", "C", "C"]) == "A"  # A and B tie; A sorts first
    assert next_format(shares, ["A", "C", "C", "C"]) == "B"
    assert next_format({"C": 1.0, "A": 0.0}, ["C"] * 9) == "C"  # a zero share never wins


def test_exploration_share_over_a_week():
    flags: list[bool] = []
    for n in range(5):
        flags.append(exploration_due(0.2, n, sum(flags)))
    assert flags == [False, False, True, False, False]  # one in five, mid-week
    flags = []
    for n in range(10):
        flags.append(exploration_due(0.2, n, sum(flags)))
    assert sum(flags) == 2 and flags.index(True) + 1 < len(flags) - flags[::-1].index(True)
    assert not any(exploration_due(0.0, n, 0) for n in range(7))


def test_recent_topics_are_skipped():
    recent = ["Never miss twice"] + ["Something else"] * 19
    assert "Never miss twice" not in texts(topic_candidates(BOOK, recent, explore=False))
    old = ["Something else"] * 20 + ["Never miss twice"]  # the 21st brief back
    offered = texts(topic_candidates(BOOK, old, explore=False))
    assert "Never miss twice" in offered
    assert offered[-1] == "Never miss twice"  # used before: after the never-used ones


def test_topics_match_whatever_the_case_or_trailing_punctuation():
    offered = texts(topic_candidates(BOOK, ["never miss twice."], explore=False))
    assert "Never miss twice" not in offered


def test_the_least_recent_pillar_comes_first():
    recent = ["Shrink the step", "Clean as you go"]  # Habits newest, then Craft; Focus unused
    offered = topic_candidates(BOOK, recent, explore=False)
    assert [t.pillar for t in offered][:3] == ["Focus", "Craft", "Habits"]


def test_each_pillar_leads_before_any_repeats():
    offered = topic_candidates(BOOK, [], explore=False, limit=3)
    assert sorted(t.pillar for t in offered) == ["Craft", "Focus", "Habits"]


def test_exploration_offers_explore_topics_only():
    offered = topic_candidates(BOOK, ["Silence as a tool"], explore=True)
    assert texts(offered) == ["Chess openings and routines"]
    assert all(t.explore for t in offered)


def test_an_exhausted_bank_offers_nothing():
    used = [t.text for t in BOOK.topics]
    assert topic_candidates(BOOK, used, explore=False) == []


@pytest.mark.parametrize("limit", [1, 2])
def test_limit_is_respected(limit):
    assert len(topic_candidates(BOOK, [], explore=False, limit=limit)) == limit
