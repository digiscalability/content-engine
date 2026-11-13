"""FR-UND-1 on this PC's RTX 5070 (Blackwell): faster-whisper large-v3 on CUDA gives word
timings within 0.1 s of a generated reference, also from a final mix with music under it.

Run with `uv run pytest -m gpu -s` (prints the measurements ADR 0009 quotes). Needs the
large-v3 model in the Hugging Face cache (about 3.1 GB, downloaded on first use).
"""

import json
import statistics
import subprocess
from pathlib import Path

import pytest

from engine.library.transcribe import (
    FasterWhisperTranscriber,
    align_words,
    refine_starts,
    speech_onsets,
)
from engine.produce.captions import voice_onsets
from engine.produce.editplan import EditPlan
from engine.produce.render_ffmpeg import RenderSources, mix_audio

pytestmark = pytest.mark.gpu

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "speech"
SPEECH = FIXTURE / "discipline.wav"
REFERENCE = json.loads((FIXTURE / "discipline.words.json").read_text(encoding="utf-8"))
VO_AT = 1.0  # where the speech sits in the final-mix plan
VRAM_LIMIT_MIB = 11 * 1024  # NFR-6


def vram_used_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return int(out.split()[0])


def start_errors(words, offset: float = 0.0) -> tuple[float, list[float]]:
    """Share of reference words matched, and |start error| per matched word."""
    reference = [w["text"] for w in REFERENCE["words"]]
    pairs = align_words(reference, words)
    errors = [abs(words[j].start - (REFERENCE["words"][i]["start"] + offset)) for i, j in pairs]
    return len(pairs) / len(reference), errors


def report(label: str, matched: float, errors: list[float]) -> None:
    print(
        f"\n{label}: matched {matched:.0%}, start error mean {statistics.mean(errors):.3f} s, "
        f"median {statistics.median(errors):.3f} s, max {max(errors):.3f} s"
    )


@pytest.fixture(scope="module")
def whisper():
    before = vram_used_mib()
    transcriber = FasterWhisperTranscriber()
    transcript = transcriber.transcribe(SPEECH)
    peak = vram_used_mib() - before
    print(f"\nVRAM for large-v3 float16 after one transcription: {peak} MiB")
    return transcriber, transcript, peak


def test_large_v3_runs_on_cuda(whisper):
    _, transcript, vram = whisper
    assert transcript.language == "en"
    assert transcript.model == "large-v3"
    assert len(transcript.words) >= 14
    assert vram < VRAM_LIMIT_MIB


def test_word_starts_within_0_1_s_of_reference(whisper):
    _, transcript, _ = whisper
    raw_matched, raw = start_errors(transcript.words)
    report("clean speech, Whisper alone", raw_matched, raw)
    refined = refine_starts(transcript.words, speech_onsets(SPEECH))
    matched, errors = start_errors(refined)
    report("clean speech, starts refined", matched, errors)
    assert matched >= 0.9
    assert statistics.mean(errors) <= 0.1
    assert max(errors) <= 0.25


def test_transcribes_the_final_mix_under_music(whisper, tmp_path):
    transcriber, _, _ = whisper
    chord = tmp_path / "chord.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.2*sin(2*PI*261.6*t)+0.2*sin(2*PI*329.6*t)+0.2*sin(2*PI*392*t):s=48000:d=10",
            str(chord),
        ],
        check=True,
    )
    plan = EditPlan.model_validate(
        {
            "timeline": [{"asset_id": 1, "in": 0, "out": 9, "role": "avatar"}],
            "audio": {
                "voice": [{"asset_id": 10, "at": VO_AT, "out": 7.38}],
                "music": {"asset_id": 11, "gain_db": -6, "duck_db": 12},
            },
            "look": {"id": "test", "version": 1},
        }
    )
    sources = RenderSources(assets={1: tmp_path / "unused.mp4", 10: SPEECH, 11: chord})
    mix = mix_audio(plan, sources, tmp_path / "work")
    transcript = transcriber.transcribe(mix)
    refined = refine_starts(transcript.words, voice_onsets(plan, sources))
    matched, errors = start_errors(refined, offset=VO_AT)
    report("final mix with music, starts refined", matched, errors)
    assert matched >= 0.9
    assert statistics.mean(errors) <= 0.1
    assert max(errors) <= 0.25
