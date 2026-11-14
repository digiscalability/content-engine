"""SRT and WebVTT subtitles as timed cues (SRD §8.2: a film's own subtitles before Whisper).

Each cue becomes one `Word` whose text is the whole cue, styling tags removed, lines joined.
"""

from __future__ import annotations

import re
from pathlib import Path

from engine.library.transcribe import Word

_TIMING = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
)
_TAGS = re.compile(r"<[^>]+>|\{\\[^}]*\}")


def _seconds(hours: str | None, minutes: str, seconds: str, millis: str) -> float:
    return round(int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
                 + int(millis.ljust(3, "0")) / 1000, 3)  # fmt: skip


def _cues(text: str) -> list[Word]:
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            match = _TIMING.search(line)
            if match is None:
                continue
            g = match.groups()
            words = " ".join(_TAGS.sub("", rest).strip() for rest in lines[i + 1 :])
            words = re.sub(r"\s+", " ", words).strip()
            if words:
                cues.append(Word(words, _seconds(*g[:4]), _seconds(*g[4:])))
            break
    return cues


def parse_srt(text: str) -> list[Word]:
    return _cues(text)


def parse_vtt(text: str) -> list[Word]:
    return _cues(text.removeprefix("﻿").removeprefix("WEBVTT"))


def parse_subtitles(path: Path) -> list[Word]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    suffix = path.suffix.lower()
    if suffix == ".srt":
        return parse_srt(text)
    if suffix == ".vtt":
        return parse_vtt(text)
    raise ValueError(f"{path.name}: only .srt and .vtt subtitles are read, not {suffix}")
