"""Transcriber interface, faster-whisper adapter and word alignment (FR-UND-1, NFR-8). No GPU."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.library.transcribe import (
    FasterWhisperTranscriber,
    Transcriber,
    Word,
    align_words,
    normalise,
)


class FakeModel:
    instances: list["FakeModel"] = []

    def __init__(self, model, device, compute_type):
        self.settings = (model, device, compute_type)
        self.calls: list[dict] = []
        FakeModel.instances.append(self)

    def transcribe(self, audio, **options):
        self.calls.append({"audio": audio, **options})
        words = [
            SimpleNamespace(word=" Discipline", start=0.12, end=0.5, probability=0.98),
            SimpleNamespace(word=" is", start=0.56, end=0.66, probability=0.99),
        ]
        segments = iter([SimpleNamespace(words=words), SimpleNamespace(words=None)])
        return segments, SimpleNamespace(language="en", language_probability=0.99)


def transcriber() -> FasterWhisperTranscriber:
    FakeModel.instances.clear()
    return FasterWhisperTranscriber(factory=FakeModel)


def test_is_a_transcriber():
    assert isinstance(transcriber(), Transcriber)


def test_transcribe_maps_segment_words(tmp_path):
    result = transcriber().transcribe(tmp_path / "mix.wav")
    assert result.language == "en"
    assert result.model == "large-v3"
    assert result.words == [
        Word("Discipline", 0.12, 0.5, 0.98),
        Word("is", 0.56, 0.66, 0.99),
    ]


def test_model_is_loaded_once_with_settings(tmp_path):
    t = transcriber()
    t.transcribe(tmp_path / "a.wav")
    t.transcribe(tmp_path / "b.wav")
    assert len(FakeModel.instances) == 1
    assert FakeModel.instances[0].settings == ("large-v3", "cuda", "float16")


def test_transcribe_asks_for_word_timestamps(tmp_path):
    t = transcriber()
    t.transcribe(tmp_path / "mix.wav")
    (call,) = FakeModel.instances[0].calls
    assert call["audio"] == str(tmp_path / "mix.wav")
    assert call["word_timestamps"] is True
    assert call["vad_filter"] is True
    assert "language" not in call  # detected, not forced


def test_normalise():
    assert normalise(" Tomorrow.") == "tomorrow"
    assert normalise("don't") == "dont"
    assert normalise("♪") == ""


def test_align_words_tolerates_spelling_and_punctuation():
    reference = ["Discipline", "is", "a", "skill", "you", "can", "practise"]
    heard = [
        Word(t, i, i + 0.5)
        for i, t in enumerate(["Discipline", "is", "a", "skill", "you", "can", "practice."])
    ]
    assert align_words(reference, heard) == [(i, i) for i in range(7)]


def test_align_words_skips_insertions():
    reference = ["start", "small", "keep"]
    heard = [Word(t, i, i + 0.5) for i, t in enumerate(["start", "um", "small", "keep"])]
    assert align_words(reference, heard) == [(0, 0), (1, 2), (2, 3)]


def test_default_paths_are_strings(tmp_path):
    t = transcriber()
    t.transcribe(Path(tmp_path / "x.wav"))
    assert isinstance(FakeModel.instances[0].calls[0]["audio"], str)


def test_cuda_dll_dirs_finds_pip_installed_cublas(tmp_path):
    from engine.library.transcribe import cuda_dll_dirs

    (tmp_path / "site" / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    (tmp_path / "site" / "nvidia" / "cudnn").mkdir(parents=True)  # no bin: skipped
    assert cuda_dll_dirs([tmp_path / "site" / "nvidia"]) == [
        tmp_path / "site" / "nvidia" / "cublas" / "bin"
    ]


def test_refine_starts_snaps_words_after_pauses_to_speech_onsets():
    from engine.library.transcribe import refine_starts

    heard = [
        Word("practise", 1.28, 1.68),
        Word("Start", 2.50, 3.06),
        Word("small", 3.06, 3.44),
        Word("keep", 3.70, 4.22),
        Word("promise", 4.42, 4.78),
        Word("and", 5.04, 5.48),
    ]
    onsets = [0.10, 2.903, 4.145, 5.473]  # where speech resumes after each pause
    refined = refine_starts(heard, onsets)
    assert [w.start for w in refined] == [1.28, 2.903, 3.06, 4.145, 4.42, 5.473]
    assert refined[5].end == pytest.approx(5.523)  # kept at least 50 ms long
    assert refined[1].end == 3.06


def test_refine_starts_leaves_words_without_an_onset_alone():
    from engine.library.transcribe import refine_starts

    heard = [Word("is", 0.52, 0.62), Word("a", 0.62, 0.72)]
    assert refine_starts(heard, [0.10, 2.903]) == heard


def test_parse_silencedetect_gives_speech_onsets():
    from engine.library.transcribe import parse_silencedetect

    stderr = (
        "[silencedetect @ 0] silence_start: 0\n"
        "[silencedetect @ 0] silence_end: 0.1 | silence_duration: 0.1\n"
        "[silencedetect @ 0] silence_start: 2.04\n[silencedetect @ 0] silence_end: 2.903 | x\n"
        "[silencedetect @ 0] silence_start: 6.639\n[silencedetect @ 0] silence_end: 7.38 | x\n"
    )
    assert parse_silencedetect(stderr, duration=7.38) == [0.1, 2.903]
    assert parse_silencedetect("[silencedetect] silence_start: 3.0\n", duration=5.0) == [0.0]
