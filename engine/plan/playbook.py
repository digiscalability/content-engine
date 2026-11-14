"""The playbook as data (SRD §6.5, §16; FR-PLN-1). The Markdown file stays what the agents
and Abbas read; this loader gives the planner its parts:

- `## Rules (Abbas-owned)`: never edited by the engine. Inside it:
  - `### Pillars`: one bullet per pillar; the name is the text before a `:` or `(`;
  - `### Banned topics and claims`: one bullet per banned topic;
  - `### Topic bank`: `#### <pillar>` groups of topic bullets, plus `#### Explore` for
    topics that try something new (FR-PLN-2).
- `## Learned (retro-owned)`: the retro's section (Phase 3).

A missing section is an error that names it, so a broken playbook never plans silently
from nothing. ADR 0017.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

EXPLORE = "explore"


class PlaybookError(ValueError):
    """The playbook is missing a section the planner needs."""


@dataclass(frozen=True)
class Topic:
    text: str
    pillar: str | None  # None for an explore topic
    explore: bool = False


@dataclass(frozen=True)
class Playbook:
    rules: str
    pillars: list[str]
    banned: list[str]
    topics: list[Topic]
    learned: str
    raw: str = field(repr=False, default="")


def _sections(text: str, level: int) -> dict[str, str]:
    """Body text of each heading of exactly `level` hashes, keyed by its title."""
    pattern = re.compile(rf"^{'#' * level} (?!#)(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    found = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        found[match.group(1).strip()] = text[match.end() : end]
    return found


def _section(sections: dict[str, str], name: str, where: str) -> str:
    for title, body in sections.items():
        if title.lower().startswith(name.lower()):
            return body
    raise PlaybookError(f"the playbook has no '{name}' section under {where}")


def _bullets(body: str) -> list[str]:
    return [line[2:].strip() for line in body.splitlines() if line.startswith("- ")]


def _pillar_name(bullet: str) -> str:
    return re.split(r"[:(]", bullet, maxsplit=1)[0].strip()


def parse_playbook(text: str) -> Playbook:
    top = _sections(text, 2)
    rules = _section(top, "Rules", "the playbook")
    learned = _section(top, "Learned", "the playbook").strip()
    parts = _sections(rules, 3)
    pillars = [_pillar_name(b) for b in _bullets(_section(parts, "Pillars", "Rules"))]
    banned = _bullets(_section(parts, "Banned topics", "Rules"))
    known = {p.lower(): p for p in pillars}
    topics = []
    for group, body in _sections(_section(parts, "Topic bank", "Rules"), 4).items():
        name = re.sub(r"\s*\*?\(.*$", "", group).strip().lower()
        if name != EXPLORE and name not in known:
            raise PlaybookError(f"topic group '{group}' matches no pillar ({', '.join(pillars)})")
        for bullet in _bullets(body):
            explore = name == EXPLORE
            topics.append(Topic(bullet, None if explore else known[name], explore))
    return Playbook(rules=rules, pillars=pillars, banned=banned, topics=topics,
                    learned=learned, raw=text)  # fmt: skip


def load_playbook(path: Path) -> Playbook:
    return parse_playbook(path.read_text(encoding="utf-8"))
