"""Text checks of a post: FR-QA-6 (banned topics and claims) and FR-QA-8 (claims about
Abbas), over the script, on-screen text, captions, titles and hashtags.

Both are deterministic floors (ADR 0013). FR-QA-6 screens whole-word terms per banned
category. FR-QA-8 fails any first-person wording (the avatar is Abbas, so "I" or "we" is an
anecdote or a claim about him), any term on the Fact Sheet's never list (read from its "Who"
section at run time, plus any extra terms the identity config adds), and any sentence
about Abbas whose role phrase, proper noun or number is not in the Public Fact Sheet. A
sentence about Abbas that cannot be checked (no Fact Sheet) holds. The qa-reviewer judges
meaning on top (FR-QA-7).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from engine.qa import CheckResult, verdict

Text = tuple[str, str]  # (where, text)

FIRST_PERSON = re.compile(
    r"\b(I|I'm|I've|I'd|I'll|me|my|mine|myself|we|we're|we've|we'll|our|ours|ourselves|us)\b",
    re.IGNORECASE,
)
SENTENCE = re.compile(r"[^.!?\n]+")
WORD = re.compile(r"[A-Za-z][A-Za-z'-]*|\d+")
STOP = {"a", "an", "the", "and", "or", "as", "is", "was", "who", "of", "his"}
PROPER = re.compile(r"\b[A-Z][a-z]+\b")
NUMBER = re.compile(r"\b\d+\b")


def _norm(text: str) -> str:
    return " " + " ".join(re.findall(r"\w+", text.lower())) + " "


def _has(text: str, term: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE) is not None


NEVER_SECTION = "Who"  # the Fact Sheet section whose never-lines are about the person
QUOTED = re.compile(r"[\"“”]([^\"“”]+)[\"“”]")
LEADING = re.compile(r"^(?:(?:a|an|the|any|current)\s+)+", re.IGNORECASE)


def _variants(item: str) -> list[str]:
    """A plural item also matches its singular ("awards", "award")."""
    if item.endswith("ies"):
        return [item, item[:-3] + "y"]
    if item.endswith("s") and not item.endswith(("ss", "us")):
        return [item, item[:-1]]
    return [item]


def parse_never_terms(markdown: str, section: str = NEVER_SECTION) -> list[str]:
    """Terms from the "Never ..." sentences of one Fact Sheet section: quoted words
    (Never "we" or "our team") and the items of a "Never mention a, b or c." list."""
    terms: list[str] = []
    inside = False
    for line in markdown.splitlines():
        if heading := re.match(r"^#{2,}\s+(.*)", line):
            inside = heading.group(1).strip().lower() == section.lower()
            continue
        if not inside:
            continue
        for sentence in re.findall(r"\bNever\b(.*?)(?:\.(?:\s|$)|$)", line):
            sentence = re.sub(r"\([^)]*\)", "", sentence)  # asides such as ("x" is false)
            terms += QUOTED.findall(sentence)
            if mention := re.match(r"\s*mention\s+(.*)", sentence):
                for item in re.split(r",|\s+or\s+|\s+and\s+", mention.group(1)):
                    item = LEADING.sub("", item.strip()).strip()
                    if item:
                        terms += _variants(item)
    return list(dict.fromkeys(terms))


@dataclass(frozen=True)
class FactSheet:
    path: Path
    text: str  # normalised: lower case, words joined by single spaces
    never_terms: list[str]  # from its "Who" section, so no copy lives in the repo

    @classmethod
    def load(cls, path: Path) -> FactSheet | None:
        if not path.is_file():
            return None
        markdown = path.read_text(encoding="utf-8")
        return cls(path, _norm(markdown), parse_never_terms(markdown))

    def says(self, phrase: str) -> bool:
        return _norm(phrase) in self.text


def post_texts(
    beats: Sequence[Mapping], caption_words: Sequence[str], copy: Sequence[Mapping] = ()
) -> list[Text]:
    """Every text a post shows or says, labelled with where it appears."""
    texts: list[Text] = [("script", " ".join(b["text"] for b in beats))]
    texts += [("on-screen text", b["on_screen"]) for b in beats if b.get("on_screen")]
    if caption_words:
        texts.append(("captions", " ".join(caption_words)))
    for c in copy:
        platform = c["platform"]
        texts += [(f"{platform} title", c["title"]), (f"{platform} caption", c["caption"])]
        if c.get("hashtags"):
            texts.append((f"{platform} hashtags", " ".join(c["hashtags"])))
    return texts


def banned_terms_check(texts: Iterable[Text], banned: Mapping[str, Sequence[str]]) -> CheckResult:
    """FR-QA-6: any banned term, whole word, in any text, is a hard fail. A hashtag also
    matches a term written without spaces (#PassiveIncome)."""
    fails = []
    for where, text in texts:
        tags = {t.lower() for t in re.findall(r"#(\w+)", text)}
        for category, terms in banned.items():
            for term in terms:
                squashed = term.replace(" ", "").lower()
                if _has(text.replace("#", " "), term) or squashed in tags:
                    fails.append(f"{where}: '{term}' is a banned {category} term (FR-QA-6)")
    return verdict("banned_topics", fails, [])


def _role_phrase(words: list[str], index: int) -> str:
    """The role word with the up to two descriptive words before it ("former chef")."""
    start = index
    while start > 0 and index - start < 2 and words[start - 1].lower() not in STOP:
        start -= 1
    return " ".join(words[start : index + 1])


def claims_check(
    texts: Iterable[Text],
    facts: FactSheet | None,
    self_terms: Sequence[str],
    never_terms: Sequence[str],
) -> CheckResult:
    """FR-QA-8: claims about Abbas match the Public Fact Sheet, or the post does not go."""
    fails: list[str] = []
    holds: list[str] = []
    never_terms = [*never_terms, *(facts.never_terms if facts else [])]
    names = {t.lower() for t in self_terms if t[:1].isupper()}  # proper names, not roles
    roles = {t.lower() for t in self_terms} - names
    for where, text in texts:
        if match := FIRST_PERSON.search(text):
            fails.append(f"{where}: '{match.group(0)}' speaks in the first person (FR-QA-8)")
        fails += [
            f"{where}: '{term}' is on the Fact Sheet's never list (FR-QA-8)"
            for term in never_terms
            if _has(text, term)
        ]
        for sentence in SENTENCE.findall(text):
            words = WORD.findall(sentence)
            lowered = [w.lower() for w in words]
            if not any(w in names or w in roles for w in lowered):
                continue
            if facts is None:
                holds.append(
                    f"{where}: '{sentence.strip()}' is about Abbas and the Public Fact Sheet "
                    "is missing, so it cannot be checked (FR-QA-8)"
                )
                continue
            claims = [_role_phrase(words, i) for i, w in enumerate(lowered) if w in roles]
            first = words[0] if words else ""
            claims += [p for p in PROPER.findall(sentence) if p.lower() not in names and p != first]
            claims += NUMBER.findall(sentence)
            fails += [
                f"{where}: '{claim}' about Abbas is not in the Public Fact Sheet (FR-QA-8)"
                for claim in claims
                if not facts.says(claim)
            ]
    return verdict("claims_about_abbas", fails, holds)
