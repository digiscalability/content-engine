"""Topic planning (SRD §6.5, FR-PLN-1, FR-PLN-2), as pure functions over the playbook and
an identity's past briefs. ADR 0017.

- `next_format`: the format furthest below its share of the recent briefs.
- `exploration_due`: whether this post explores, keeping a week near the identity's
  exploration share (rounded half up, so 0.2 of five posts is the third post).
- `topic_candidates`: bank topics not used in the last `WINDOW` briefs; never-used topics
  first, then the pillar used least recently, one per pillar before any repeats.

A brief counts as using a bank topic when its topic text matches, ignoring case, spacing
and trailing punctuation; the prompts ask the agents to copy the text as offered.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence

from engine.plan.playbook import Playbook, Topic

WINDOW = 20  # FR-PLN-3: a post differs from the identity's last 20


def normalise(topic: str) -> str:
    return re.sub(r"\s+", " ", topic).strip().rstrip(".!?…").strip().casefold()


def next_format(shares: Mapping[str, float], recent_formats: Sequence[str]) -> str:
    """The format with the largest gap between its share and its count so far."""
    live = {f: s for f, s in shares.items() if s > 0}
    if not live:
        raise ValueError("no format has a share above zero")
    total = len(recent_formats) + 1
    counts = {f: sum(1 for r in recent_formats if r == f) for f in live}
    return min(live, key=lambda f: (-(live[f] * total - counts[f]), -live[f], f))


def exploration_due(share: float, briefs_this_week: int, explored_this_week: int) -> bool:
    """True when the week, counting this post, owes another exploration post."""
    owed = math.floor((briefs_this_week + 1) * share + 0.5 + 1e-9)
    return explored_this_week < owed


def _spread(topics: list[Topic]) -> list[Topic]:
    """The first topic of each pillar, in order, then the rest in order."""
    seen: set[str | None] = set()
    leads, rest = [], []
    for topic in topics:
        (rest if topic.pillar in seen else leads).append(topic)
        seen.add(topic.pillar)
    return leads + rest


def topic_candidates(
    book: Playbook,
    history: Sequence[str],
    explore: bool,
    limit: int = 5,
    window: int = WINDOW,
) -> list[Topic]:
    """Bank topics to offer, best first. `history` is the identity's brief topics, newest
    first."""
    past = [normalise(t) for t in history]
    recent = set(past[:window])
    last_use = {}  # normalised topic -> index of its newest use (0 = the latest brief)
    for i, topic in enumerate(past):
        last_use.setdefault(topic, i)
    pillar_age = {}
    for topic in book.topics:
        if topic.pillar and (i := last_use.get(normalise(topic.text))) is not None:
            pillar_age[topic.pillar] = min(pillar_age.get(topic.pillar, i), i)
    never = len(past) + 1  # older than anything used

    pool = [
        (n, t)
        for n, t in enumerate(book.topics)
        if t.explore == explore and normalise(t.text) not in recent
    ]

    def key(item: tuple[int, Topic]) -> tuple:
        n, topic = item
        age = pillar_age.get(topic.pillar, never) if topic.pillar else never
        own = last_use.get(normalise(topic.text), never)
        return (-age, -own, n)

    fresh = [t for n, t in sorted(pool, key=key) if normalise(t.text) not in last_use]
    reused = [t for n, t in sorted(pool, key=key) if normalise(t.text) in last_use]
    return (_spread(fresh) + _spread(reused))[:limit]
