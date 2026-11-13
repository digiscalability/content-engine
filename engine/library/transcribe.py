"""Speech to word timings (SRD FR-UND-1): faster-whisper large-v3 on CUDA.

Used for ingest (transcripts of own footage) and for captions, which come from the
final audio mix (FR-PRD-6). The model loads lazily, once per transcriber, through an
injectable factory so tests never touch the GPU (NFR-8). Blackwell results: ADR 0009.
"""

from __future__ import annotations

import difflib
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from engine.produce.ffmpeg import wav_duration
from engine.produce.process import Runner, output_tail, run_command

_NOT_WORD = re.compile(r"[^\w]+", re.UNICODE)


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    probability: float | None = None


@dataclass(frozen=True)
class Transcript:
    language: str
    model: str
    words: list[Word]


@runtime_checkable
class Transcriber(Protocol):
    def transcribe(self, audio: Path) -> Transcript:
        """Word-level timings for one audio file."""
        ...


@dataclass(frozen=True)
class ScriptAlignedTranscriber:
    """Dry runs and tests: the script's own words spread evenly over the audio. The fake
    voice is silent, so there is nothing to hear; the captions still get real timings."""

    text: str
    lead: float = 0.1  # caption rule: nothing before 0.1 s
    tail: float = 0.1
    gap: float = 0.02

    def transcribe(self, audio: Path) -> Transcript:
        tokens = self.text.split()
        span = max(wav_duration(audio) - self.lead - self.tail, 0.1) / max(len(tokens), 1)
        words = [
            Word(
                token,
                round(self.lead + i * span, 3),
                round(self.lead + (i + 1) * span - self.gap, 3),
                1.0,
            )
            for i, token in enumerate(tokens)
        ]
        return Transcript(language="en", model="script-aligned", words=words)


def cuda_dll_dirs(nvidia_paths: Sequence[Path] | None = None) -> list[Path]:
    """`bin` folders of pip-installed NVIDIA libraries (`nvidia-cublas-cu12`).

    CTranslate2's Windows wheel bundles cuDNN but not cuBLAS, which it loads by name at
    the first CUDA call; these folders have to be on the DLL search path first (ADR 0009).
    """
    if nvidia_paths is None:
        try:
            import nvidia  # namespace package from the nvidia-* wheels
        except ImportError:
            return []
        nvidia_paths = [Path(p) for p in nvidia.__path__]
    return sorted(
        lib / "bin"
        for base in nvidia_paths
        for lib in Path(base).iterdir()
        if (lib / "bin").is_dir()
    )


def _prepare_cuda_dlls() -> None:
    if os.name != "nt":
        return
    for directory in cuda_dll_dirs():
        os.add_dll_directory(str(directory))
        if str(directory) not in os.environ.get("PATH", ""):
            os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"


def _default_factory(model: str, device: str, compute_type: str) -> Any:
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    if device == "cuda":
        _prepare_cuda_dlls()
    from faster_whisper import WhisperModel

    return WhisperModel(model, device=device, compute_type=compute_type)


@dataclass
class FasterWhisperTranscriber:
    model: str = "large-v3"
    device: str = "cuda"
    compute_type: str = "float16"
    factory: Callable[[str, str, str], Any] | None = None
    _loaded: Any = field(default=None, init=False, repr=False)

    def _whisper(self) -> Any:
        if self._loaded is None:
            factory = self.factory or _default_factory
            self._loaded = factory(self.model, self.device, self.compute_type)
        return self._loaded

    def transcribe(self, audio: Path) -> Transcript:
        segments, info = self._whisper().transcribe(
            str(audio), word_timestamps=True, vad_filter=True, beam_size=5
        )
        words = [
            Word(w.word.strip(), round(w.start, 3), round(w.end, 3), w.probability)
            for segment in segments
            for w in (segment.words or [])
            if w.word.strip()
        ]
        return Transcript(language=info.language, model=self.model, words=words)


_SILENCE_START = re.compile(r"silence_start: (-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end: (-?[\d.]+)")
_DURATION = re.compile(r"Duration: (\d+):(\d+):([\d.]+)")


def parse_silencedetect(stderr: str, duration: float) -> list[float]:
    """Times where speech starts or resumes, from ffmpeg `silencedetect` output."""
    starts = [float(v) for v in _SILENCE_START.findall(stderr)]
    onsets = [] if starts and starts[0] < 0.001 else [0.0]
    onsets += [e for e in map(float, _SILENCE_END.findall(stderr)) if e < duration - 0.01]
    return onsets


def speech_onsets(
    audio: Path, noise_db: float = -40, min_silence: float = 0.12, run: Runner = run_command
) -> list[float]:
    """Speech onsets in a clean voice track, by energy (accurate to about 15 ms)."""
    detect = f"silencedetect=noise={noise_db}dB:d={min_silence}"
    result = run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio), "-af", detect, "-f", "null", "-"],
        None,
    )
    if result.returncode != 0:
        raise RuntimeError(f"silencedetect failed on {audio}: {output_tail(result)}")
    h, m, s = _DURATION.search(result.stderr).groups()
    return parse_silencedetect(result.stderr, int(h) * 3600 + int(m) * 60 + float(s))


def refine_starts(
    words: Sequence[Word],
    onsets: Sequence[float],
    tolerance: float = 0.05,
    min_length: float = 0.05,
) -> list[Word]:
    """Move a word's start to the speech onset inside it.

    Whisper's word alignment gives the word after a pause the pause's start time, so a
    caption would appear up to half a second early. When speech resumes inside a word's
    span (or up to `tolerance` after its end), the word starts there instead (ADR 0009).
    """
    refined = []
    for word in words:
        inside = [s for s in onsets if word.start < s <= word.end + tolerance]
        if inside:
            start = round(max(inside), 3)
            end = round(max(word.end, start + min_length), 3)
            word = Word(word.text, start, end, word.probability)
        refined.append(word)
    return refined


def normalise(text: str) -> str:
    """Lower case, letters and digits only: how words are compared, never displayed."""
    return _NOT_WORD.sub("", text.lower()).replace("_", "")


def align_words(reference: Sequence[str], words: Sequence[Word]) -> list[tuple[int, int]]:
    """Pair reference words with transcribed words by sequence (indices into each).

    Exact matches pair up; a one-for-one replacement (a spelling variant such as
    "practise"/"practice") pairs too; insertions, deletions and uneven replacements don't.
    """
    ref = [normalise(t) for t in reference]
    heard = [normalise(w.text) for w in words]
    pairs: list[tuple[int, int]] = []
    matcher = difflib.SequenceMatcher(None, ref, heard, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            pairs += list(zip(range(i1, i2), range(j1, j2), strict=True))
    return pairs
